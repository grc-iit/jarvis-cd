cmake_minimum_required(VERSION 3.18)
project(gray_scott LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CUDA_STANDARD 17)

find_package(MPI REQUIRED)
find_package(HDF5 REQUIRED COMPONENTS C)

add_executable(gray_scott gray_scott.cu)

target_link_libraries(gray_scott PRIVATE MPI::MPI_CXX ${HDF5_C_LIBRARIES})
target_include_directories(gray_scott PRIVATE ${HDF5_C_INCLUDE_DIRS})

# Allow caller to set CUDA arch via env or cmake arg
if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES "80" CACHE STRING "CUDA architectures")
endif()

set_target_properties(gray_scott PROPERTIES
    CUDA_SEPARABLE_COMPILATION ON
)
