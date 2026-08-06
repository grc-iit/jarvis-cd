# Installation

The installed-executable profile requires `gray-scott-analyze` on `PATH` with an ADIOS2 runtime.

The source-bundle profile requires CMake, an MPI C++ compiler, and ADIOS2 development libraries. JARVIS verifies and stages the complete source bundle in package-owned storage, configures it with CMake, and builds only the `gray-scott-analyze` target.
