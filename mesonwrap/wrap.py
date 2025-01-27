from collections import namedtuple


def _base_name(name: str, version: str, revision: int) -> str:
    return '{name}-{version}-{revision}-wrap'.format(
        name=name, version=version, revision=revision)


def wrap_name(name: str, version: str, revision: int) -> str:
    return _base_name(name, version, revision) + '.wrap'


def zip_name(name: str, version: str, revision: int) -> str:
    return _base_name(name, version, revision) + '.zip'


_Wrap = namedtuple(
    'Wrap',
    ['name', 'version', 'revision', 'wrap', 'zip'],
)


class Wrap(_Wrap):

    @property
    def wrap_name(self):
        return wrap_name(self.name, self.version, self.revision)

    @property
    def zip_name(self):
        return zip_name(self.name, self.version, self.revision)
