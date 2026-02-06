cmake_minimum_required(VERSION 3.14)
project(llm-service LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)

# Optimization flags
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -march=native -DNDEBUG")
set(CMAKE_C_FLAGS_RELEASE "-O3 -march=native -DNDEBUG")

# llama.cpp as subdirectory
set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON ON CACHE BOOL "" FORCE)
set(GGML_CUDA OFF CACHE BOOL "" FORCE)
add_subdirectory(lib/llama.cpp)

# Our service
add_executable(llm-service
    src/main.cpp
    src/inference.cpp
)

target_include_directories(llm-service PRIVATE
    lib                     # httplib.h, json.hpp
    lib/llama.cpp/include
    lib/llama.cpp/common    # common.h
    src
)

target_link_libraries(llm-service PRIVATE
    llama
    common
    pthread
)

target_compile_definitions(llm-service PRIVATE
    CPPHTTPLIB_OPENSSL_SUPPORT=0
)
