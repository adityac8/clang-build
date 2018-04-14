'''
Target describes a single build or dependency target with all needed paths and
a list of buildables that comprise it's compile and link steps.
'''

import os as _os
from pathlib2 import Path as _Path
import subprocess as _subprocess
from multiprocessing import freeze_support as _freeze_support
import logging as _logging

from . import platform as _platform
from .dialect_check import get_dialect_string as _get_dialect_string
from .dialect_check import get_max_supported_compiler_dialect as _get_max_supported_compiler_dialect
from .build_type import BuildType as _BuildType
from .single_source import SingleSource as _SingleSource

_LOGGER = _logging.getLogger('clang_build.clang_build')

class Target:
    DEFAULT_COMPILE_FLAGS = ['-Wall', '-Werror']
    DEFAULT_RELEASE_COMPILE_FLAGS = ['-O3', '-DNDEBUG']
    DEFAULT_DEBUG_COMPILE_FLAGS = ['-O0', '-g3', '-DDEBUG']
    DEFAULT_COVERAGE_COMPILE_FLAGS = (
        DEFAULT_DEBUG_COMPILE_FLAGS +
        ['--coverage',
         '-fno-inline',
         '-fno-inline-small-functions',
         '-fno-default-inline'])


    def __init__(self,
            name,
            rootDirectory,
            buildDirectory,
            headers,
            include_directories,
            buildType,
            clangpp,
            options=None,
            dependencies=None):

        if options is None:
            options = {}
        if dependencies is None:
            dependencies = []

        self.dependencyTargets = dependencies

        # Basics
        self.name          = name
        self.rootDirectory = rootDirectory
        self.root          = _Path('')
        self.buildType     = buildType

        self.buildDirectory = buildDirectory.joinpath(buildType.name.lower())

        self.headers = headers

        self.includeDirectories = []
        if rootDirectory.joinpath('include').exists():
            self.includeDirectories.append(rootDirectory.joinpath('include'))
        self.includeDirectories += include_directories

        if 'properties' in options and 'cpp_version' in options['properties']:
            self.dialect = _get_dialect_string(options['properties']['cpp_version'])
        else:
            self.dialect = _get_max_supported_compiler_dialect(clangpp)

        # TODO: parse user-specified target version

        # If target is marked as external, try to fetch the sources
        self.external = options.get('external', False)
        if self.external:
            downloaddir = buildDirectory.joinpath('external_sources')
            # Check if directory is already present and non-empty
            if downloaddir.exists() and _os.listdir(str(downloaddir)):
                _LOGGER.info(f'External target {self.name}: sources found in {str(downloaddir)}')
            # Otherwise we download the sources
            else:
                _LOGGER.info(f'External target {self.name}: downloading to {str(downloaddir)}')
                downloaddir.mkdir(parents=True, exist_ok=True)
                _subprocess.call(["git", "clone", options["url"], str(downloaddir)])
                _LOGGER.info(f'External target {self.name}: downloaded')
            self.includeDirectories.append(downloaddir)
            self.rootDirectory = downloaddir

        compileFlags        = Target.DEFAULT_COMPILE_FLAGS
        compileFlagsDebug   = Target.DEFAULT_DEBUG_COMPILE_FLAGS
        compileFlagsRelease = Target.DEFAULT_RELEASE_COMPILE_FLAGS
        self.linkFlags = []

        if 'flags' in options:
            compileFlags += options['flags'].get('compile', [])
            compileFlagsRelease += options['flags'].get('compileRelease', [])
            compileFlagsDebug += options['flags'].get('compileDebug', [])
            self.linkFlags += options['flags'].get('link', [])

        self.compileFlags = compileFlags
        if self.buildType == _BuildType.Release:
            self.compileFlags += compileFlagsRelease
        if self.buildType == _BuildType.Debug:
            self.compileFlags += compileFlagsDebug

        for target in self.dependencyTargets:
            self.compileFlags += target.compileFlags
            self.includeDirectories += target.includeDirectories
            self.headers += target.headers

        self.compileFlags = list(set(self.compileFlags))
        self.includeDirectories = list(set(self.includeDirectories))
        self.headers = list(set(self.headers))

    def get_include_directory_command(self):
        return [f'-I{dir}' for dir in self.includeDirectories]

    def link(self):
        # Subclasses must implement
        raise NotImplementedError()

    def compile(self, process_pool):
        # Subclasses must implement
        raise NotImplementedError()

    def check_for_unsuccesful_builds(self):
        # Subclasses must implement
        raise NotImplementedError()

class HeaderOnly(Target):
    def link(self):
        _LOGGER.info(f'Header-only target {self.name} does not require linking.')

    def compile(self, process_pool):
        _LOGGER.info(f'Header-only target {self.name} does not require compiling.')

    def check_for_unsuccesful_builds(self):
        return False

def generateDepfile(buildable):
    buildable.generate_dependency_file()

def compile_single_source(buildable):
    buildable.compile()

class Compilable(Target):

    def __init__(self,
            name,
            rootDirectory,
            buildDirectory,
            headers,
            include_directories,
            source_files,
            buildType,
            clangpp,
            link_command,
            output_folder,
            platform_flags,
            prefix,
            suffix,
            options=None,
            dependencies=None):

        if not source_files:
            error_message = f'ERROR: Targt {name} was defined as a {self.__class__} but no source files were found'
            _LOGGER.error(error_message)
            raise RuntimeError(error_message)

        super().__init__(
            name=name,
            rootDirectory=rootDirectory,
            buildDirectory=buildDirectory,
            headers=headers,
            include_directories=include_directories,
            buildType=buildType,
            clangpp=clangpp,
            options=options,
            dependencies=dependencies)

        if options is None:
            options = {}
        if dependencies is None:
            dependencies = []

        self.objectDirectory     = self.buildDirectory.joinpath('obj')
        self.depfileDirectory    = self.buildDirectory.joinpath('dep')
        self.outputFolder        = self.buildDirectory.joinpath(output_folder)

        self.objectDirectory.mkdir(parents=True, exist_ok=True)
        self.depfileDirectory.mkdir(parents=True, exist_ok=True)
        self.outputFolder.mkdir(parents=True, exist_ok=True)

        if 'output_name' in options:
            self.outname = options['output_name']
        else:
            self.outname = self.name

        self.outfile = _Path(self.outputFolder, prefix + self.outname + suffix)


        # Clang
        self.clangpp   = clangpp

        # Sources
        self.sourceFiles        = source_files

        # Buildables which this Target contains
        self.buildables = [_SingleSource(
            sourceFile=sourceFile,
            platformFlags=platform_flags,
            current_target_root_path=self.rootDirectory.joinpath(self.root),
            depfileDirectory=self.depfileDirectory,
            objectDirectory=self.objectDirectory,
            include_strings=self.get_include_directory_command(),
            compileFlags=self.compileFlags,
            clangpp=self.clangpp) for sourceFile in self.sourceFiles]

        # If compilation of buildables fail, they will be stored here later
        self.unsuccesful_builds = []

        # Linking setup
        self.linkCommand = link_command + [str(self.outfile)]

        ### Library dependency search paths
        for target in self.dependencyTargets:
            if not target.__class__ is HeaderOnly:
                self.linkCommand += ['-L'+str(target.outputFolder.resolve())]

        ### Include directories
        #linkCommand += self.get_include_directory_command()

        ### Link self
        self.linkCommand += [str(buildable.objectFile) for buildable in self.buildables]

        ### Link dependencies
        for target in self.dependencyTargets:
            if not target.__class__ is HeaderOnly:
                self.linkCommand += ['-l'+target.outname]

        ### Additional scripts
        self.beforeCompileScript = ""
        self.beforeLinkScript    = ""
        self.afterBuildScript    = ""
        if 'scripts' in options: ### TODO: maybe the scripts should be named differently
            if 'before_compile' in config['scripts']:
                self.beforeCompileScript = _Path(self.rootDirectory, "/", config['scripts']['before_compile'])
                self.beforeLinkScript    = _Path(self.rootDirectory, "/", config['scripts']['before_link'])
                self.afterBuildScript    = _Path(self.rootDirectory, "/", config['scripts']['after_build'])

    # From the list of source files, compile those which changed or whose dependencies (included headers, ...) changed
    def compile(self, process_pool):
        # Object file only needs to be (re-)compiled if the source file or headers it depends on changed
        self.neededBuildables = [buildable for buildable in self.buildables if buildable.needs_rebuild]

        # If the target was not modified, it may not need to compile
        if not self.neededBuildables:
            _LOGGER.info(f'Target [{self.name}] is already compiled')
            return

        _LOGGER.info(f'Target [{self.name}] needs to rebuild sources %s', [b.name for b in self.neededBuildables])

        # Before-compile step
        if self.beforeCompileScript and not self.compiled:
            _LOGGER.info(f'Pre-compile step of target [{self.name}]')
            originalDir = os.getcwd()
            newDir, _ = os.path.split(self.beforeCompileScript)
            os.chdir(newDir)
            execfile(self.beforeCompileScript)
            os.chdir(originalDir)
            _LOGGER.info(f'Finished pre-compile step of target [{self.name}]')

        # Compile

        # Execute compile command
        _LOGGER.info(f'Compile target [{self.name}]')
        process_pool.map_async(compile_single_source, self.neededBuildables)


    def check_for_unsuccesful_builds(self):
        self.unsuccesful_builds = [buildable for buildable in self.neededBuildables if buildable.compilation_failed]
        return len(self.unsuccesful_builds) > 0

    def link(self):
        # Before-link step
        if self.beforeLinkScript:
            _LOGGER.info(f'Pre-link step of target [{self.name}]')
            originalDir = os.getcwd()
            newDir, _ = os.path.split(self.beforeCompileScript)
            os.chdir(newDir)
            execfile(self.beforeLinkScript)
            os.chdir(originalDir)
            _LOGGER.info(f'Finished pre-link step of target [{self.name}]')
        # Execute link command
        _LOGGER.info(f'Link target [{self.name}]')
        # TODO: Capture output
        _LOGGER.debug('    ' + ' '.join(self.linkCommand))
        _subprocess.call(self.linkCommand)
        # After-build step
        if self.afterBuildScript:
            _LOGGER.info(f'After-build step of target [{self.name}]')
            originalDir = os.getcwd()
            newDir, _ = os.path.split(self.beforeCompileScript)
            os.chdir(newDir)
            execfile(self.afterBuildScript)
            os.chdir(originalDir)
            _LOGGER.info(f'Finished after-build step of target [{self.name}]')


class Executable(Compilable):
    def __init__(self,
            name,
            rootDirectory,
            buildDirectory,
            headers,
            include_directories,
            source_files,
            buildType,
            clangpp,
            options=None,
            dependencies=None):

        super().__init__(
            name=name,
            rootDirectory=rootDirectory,
            buildDirectory=buildDirectory,
            headers=headers,
            include_directories=include_directories,
            source_files=source_files,
            buildType=buildType,
            clangpp=clangpp,
            link_command=[clangpp, '-o'],
            output_folder = _platform.EXECUTABLE_OUTPUT,
            platform_flags=_platform.PLATFORM_EXTRA_FLAGS_EXECUTABLE,
            prefix=_platform.EXECUTABLE_PREFIX,
            suffix=_platform.EXECUTABLE_SUFFIX,
            options=options,
            dependencies=dependencies)


class SharedLibrary(Compilable):
    def __init__(self,
            name,
            rootDirectory,
            buildDirectory,
            headers,
            include_directories,
            source_files,
            buildType,
            clangpp,
            options=None,
            dependencies=None):

        super().__init__(
            name=name,
            rootDirectory=rootDirectory,
            buildDirectory=buildDirectory,
            headers=headers,
            include_directories=include_directories,
            source_files=source_files,
            buildType=buildType,
            clangpp=clangpp,
            link_command=[clangpp, '-shared', '-o'],
            output_folder = _platform.SHARED_LIBRARY_OUTPUT,
            platform_flags=_platform.PLATFORM_EXTRA_FLAGS_SHARED,
            prefix=_platform.SHARED_LIBRARY_PREFIX,
            suffix=_platform.SHARED_LIBRARY_SUFFIX,
            options=options,
            dependencies=dependencies)


class StaticLibrary(Compilable):
    def __init__(self,
            name,
            rootDirectory,
            buildDirectory,
            headers,
            include_directories,
            source_files,
            buildType,
            clangpp,
            clang_ar,
            options=None,
            dependencies=None):

        super().__init__(
            name=name,
            rootDirectory=rootDirectory,
            buildDirectory=buildDirectory,
            headers=headers,
            include_directories=include_directories,
            source_files=source_files,
            buildType=buildType,
            clangpp=clangpp,
            link_command=[clang_ar, 'rc'],
            output_folder = _platform.STATIC_LIBRARY_OUTPUT,
            platform_flags=_platform.PLATFORM_EXTRA_FLAGS_SHARED,
            prefix=_platform.SHARED_LIBRARY_PREFIX,
            suffix=_platform.SHARED_LIBRARY_SUFFIX,
            options=options,
            dependencies=dependencies)

if __name__ == '__main__':
    _freeze_support()
