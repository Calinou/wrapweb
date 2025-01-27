# Copyright 2015 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import sys
from typing import Optional, Tuple
import urllib.request

import git
import github

from mesonwrap import tempfile
from mesonwrap import upstream
from mesonwrap import wrapcreator
from mesonwrap.tools import environment


class CheckError(Exception):
    pass


def print_status(msg, check: bool, fatal: bool = True, quiet: bool = False):
    """Prints msg with success indicator based on check parameter.
    Args:
        msg: str, status message to print
        check: success of the check
        fatal: if exception should be raised
        quiet: if message should be printed on success
    Raises: CheckError(msg) if not check and fatal
    """
    OK_CHR = '\u2611'
    FAIL_CHR = '\u2612'
    status = OK_CHR if check else FAIL_CHR
    if not quiet or not check:
        print('{msg}: {status}'.format(msg=msg, status=status))
    if not check and fatal:
        raise CheckError(msg)


class Reviewer:

    @staticmethod
    def _get_project(organization: str, project: str
                    ) -> github.Repository.Repository:
        gh = environment.Github()
        org = gh.get_organization('mesonbuild')
        return org.get_repo(project)

    @classmethod
    def from_pull_request(cls, organization: str, project: str, pull_id: int):
        pull = cls._get_project(organization, project).get_pull(pull_id)
        return cls(project=project, clone_url=pull.head.repo.clone_url,
                   branch=pull.base.ref, source_branch=pull.head.ref)

    @classmethod
    def from_committed(cls, organization: str, project: str, branch: str):
        return cls(project=project,
                   clone_url=cls._get_project(organization, project).clone_url,
                   branch=branch)

    @classmethod
    def from_repository(cls, project, clone_url, branch):
        return cls(project=project, clone_url=clone_url, branch=branch)

    def __init__(self, project, clone_url, branch, source_branch=None):
        self._project = project
        self._clone_url = clone_url
        self._branch = branch
        self._source_branch = source_branch or branch
        self.strict_fileset = True
        self.strict_version_in_url = True

    def review(self, export_sources=None) -> Tuple[bool, Optional[str]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            r = self.review_int(tmpdir)
            if export_sources:
                shutil.copytree(os.path.join(tmpdir, 'src'), export_sources)
            return r

    def review_int(self, tmpdir) -> Tuple[bool, Optional[str]]:
        head_dir = os.path.join(tmpdir, 'head')
        with contextlib.closing(
                git.Repo.clone_from(self._clone_url, head_dir,
                                    branch=self._source_branch)) as head_repo:
            try:
                self.check_basics(head_repo)
                self.check_files(head_dir)
                upwrap = upstream.UpstreamWrap.from_file(
                    os.path.join(head_dir, 'upstream.wrap'))
                self.check_wrapformat(upwrap)
                self.check_url(upwrap)
                self.check_download(tmpdir, upwrap)
                self.check_extract(tmpdir, upwrap)
                self.check_build(tmpdir, upwrap)
                return (True, head_repo.head.object.hexsha)
            except CheckError:
                return (False, None)

    @staticmethod
    def check_has_no_path_separators(name, value):
        print_status(name + ' has no path separators',
                     '/' not in value and '\\' not in value)

    def check_wrapformat(self, upwrap):
        print_status('upstream.wrap has directory', upwrap.has_directory)
        self.check_has_no_path_separators('upstream.wrap directory',
                                          upwrap.directory)
        print_status('upstream.wrap has source_url', upwrap.has_source_url)
        print_status('upstream.wrap has source_filename',
                     upwrap.has_source_filename)
        self.check_has_no_path_separators('upstream.wrap source_filename',
                                          upwrap.source_filename)
        print_status('upstream.wrap has source_hash', upwrap.has_source_hash)

    def check_url(self, upwrap):
        print_status('upstream.wrap has source_url with version substring',
                     self._branch in upwrap.source_url,
                     fatal=self.strict_version_in_url)

    def check_files(self, head_dir):
        found = False
        permitted_files = ['upstream.wrap', 'meson.build', 'readme.txt',
                           'meson_options.txt', '.gitignore', 'LICENSE.build']
        for root, dirs, files in os.walk(head_dir):
            if '.git' in dirs:
                dirs.remove('.git')
            for fname in files:
                if fname not in permitted_files:
                    if not found:
                        print('Non-buildsystem files found:')
                    found = True
                    abs_name = os.path.join(root, fname)
                    rel_name = abs_name[len(head_dir) + 1:]
                    print(' ', rel_name)
        print_status('Repo contains only buildsystem files', not found,
                     fatal=self.strict_fileset)

    @staticmethod
    def isfile(head_dir, filename):
        return os.path.isfile(os.path.join(head_dir, filename))

    def check_basics(self, head_repo):
        print('Inspecting project %s, branch %s.' %
              (self._project, self._branch))
        head_dir = head_repo.working_dir
        print_status('Repo name valid',
                     re.fullmatch('[a-z][a-z0-9._-]*', self._project))
        print_status('Branch name valid',
                     re.fullmatch('[a-z0-9._]+', self._branch))
        print_status('Target branch is not master', self._branch != 'master')
        print_status('Has readme.txt', self.isfile(head_dir, 'readme.txt'))
        print_status('Has LICENSE.build',
                     self.isfile(head_dir, 'LICENSE.build'))
        print_status('Has upstream.wrap',
                     self.isfile(head_dir, 'upstream.wrap'))

    @staticmethod
    def _fetch(url):
        data = None
        exc = None
        try:
            with urllib.request.urlopen(url) as u:
                data = u.read()
        except Exception as e:
            exc = e
        return (data, exc)

    def check_download(self, tmpdir, upwrap):
        source_data, download_exc = self._fetch(upwrap.source_url)
        try:
            print_status('Download URL works', download_exc is None)
        except CheckError:
            print(' error:', str(download_exc))
            raise
        with open(os.path.join(tmpdir, upwrap.source_filename), 'wb') as f:
            f.write(source_data)
        h = hashlib.sha256()
        h.update(source_data)
        calculated_hash = h.hexdigest()
        try:
            print_status('Hash matches', calculated_hash == upwrap.source_hash)
        except CheckError:
            print(' expected:', upwrap.source_hash)
            print('      got:', calculated_hash)
            raise

    @staticmethod
    def mergetree(src, dst, ignore=None):
        for dirpath, dirnames, filenames in os.walk(src):
            prefix = os.path.relpath(dirpath, src)
            dstpath = os.path.join(dst, prefix)
            try:
                del dirnames[dirnames.index('.git')]
            except ValueError:
                pass  # it's fine if there is no .git directory
            for d in dirnames:
                os.makedirs(os.path.join(dstpath, d), exist_ok=True)
            for f in filenames:
                if f in ('readme.txt', 'upstream.wrap'):
                    continue
                dest = os.path.join(dstpath, f)
                print_status('{!r} already exists'.format(
                                 os.path.join(prefix, f)),
                             not os.path.exists(dest),
                             quiet=True)
                shutil.copy2(os.path.join(dirpath, f), dest)
        return True

    def check_extract(self, tmpdir, upwrap):
        srcdir = os.path.join(tmpdir, 'src')
        srcarchive = os.path.join(tmpdir, upwrap.source_filename)
        os.mkdir(srcdir)
        if upwrap.has_lead_directory_missing:
            os.mkdir(os.path.join(srcdir, upwrap.directory))
            shutil.unpack_archive(srcarchive,
                                  os.path.join(srcdir, upwrap.directory))
        else:
            shutil.unpack_archive(srcarchive, srcdir)
        srcdir = os.path.join(srcdir, upwrap.directory)
        print_status('upstream.wrap directory {!r} exists'.format(
                         upwrap.directory),
                     os.path.exists(srcdir))
        print_status('Patch merges with source',
                     self.mergetree(os.path.join(tmpdir, 'head'), srcdir))

    def check_build(self, tmpdir, upwrap):
        srcdir = os.path.join(tmpdir, 'src', upwrap.directory)
        bindir = os.path.join(tmpdir, 'bin')
        setup_result = subprocess.call(['meson', 'setup', srcdir, bindir])
        print_status('meson setup', setup_result == 0)
        test_result = subprocess.call(['ninja', '-C', bindir, 'test'])
        print_status('ninja test', test_result == 0)

    @classmethod
    def merge(cls, organization: str, project: str, pull_id: int, sha: str
             ) -> str:
        pull_request = cls._get_project(organization, project).get_pull(pull_id)
        method = 'squash' if pull_request.commits > 1 else 'rebase'
        branch = pull_request.base.ref
        pull_request.merge(merge_method=method, sha=sha)
        return branch

    @classmethod
    def publish(cls, organization: str, project: str, branch: str):
        gh_project = cls._get_project(organization, project)
        url = gh_project.clone_url
        wrap = wrapcreator.make_wrap(project, url, branch)
        # TODO actually publish
        raise NotImplementedError('does not publish yet')


def main(prog, args):
    parser = argparse.ArgumentParser(prog)
    parser.add_argument('name')
    parser.add_argument('--pull_request', type=int)
    parser.add_argument('--branch')
    parser.add_argument('--clone_url')
    parser.add_argument('--allow_other_files', action='store_true')
    parser.add_argument('--allow_url_without_version', action='store_true')
    parser.add_argument('--export_sources')
    parser.add_argument('--approve', action='store_true',
                        help='Approve and admit revision into WrapDB')
    parser.add_argument('--publish', action='store_true',
                        help='Publish wrap to Github')
    parser.add_argument('--test', action='store_const', const='mesonbuild-test',
                        dest='organization', default='mesonbuild',
                        help='Use mesonbuild-test organization')
    args = parser.parse_args(args)
    if args.pull_request:
        r = Reviewer.from_pull_request(args.organization,
                                       args.name, args.pull_request)
    elif args.branch:
        if args.clone_url:
            r = Reviewer.from_repository(args.name, args.clone_url,
                                         args.branch)
        else:
            r = Reviewer.from_committed(args.organization,
                                        args.name, args.branch)
    else:
        sys.exit('Either --pull_request or --branch must be set')
    r.strict_fileset = not args.allow_other_files
    r.strict_version_in_url = not args.allow_url_without_version
    review, sha = r.review(args.export_sources)
    if not review:
        sys.exit(1)
    if args.approve:
        if args.pull_request is None:
            sys.exit('Must specify --approve and --pull_request together')
        version = Reviewer.merge(args.organization, args.name,
                                 args.pull_request, sha)
        if args.publish:
            Reviewer.publish(args.organization, args.name, version)
