# Builds libvoiceassist_llama.so: llama.cpp (pinned) + the JNI bridge.
# Used by the Android app (externalNativeBuild) and by the desktop JVM tests.
cmake_minimum_required(VERSION 3.22)
project(voiceassist_llama CXX C)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(LLAMA_CPP_TAG "b12609" CACHE STRING "llama.cpp release tag")
set(LLAMA_CPP_DIR "" CACHE PATH "Use a local llama.cpp checkout instead of downloading")

set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLAMA_CURL OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
set(GGML_LLAMAFILE OFF CACHE BOOL "" FORCE)
if(ANDROID)
  set(GGML_NATIVE OFF CACHE BOOL "" FORCE)  # don't tune for the build machine
endif()

if(LLAMA_CPP_DIR)
  add_subdirectory(${LLAMA_CPP_DIR} llama.cpp-build)
else()
  include(FetchContent)
  # (no DOWNLOAD_EXTRACT_TIMESTAMP: the Android SDK ships CMake 3.22, which doesn't know that option)
  FetchContent_Declare(llama_cpp
    URL https://github.com/ggml-org/llama.cpp/archive/refs/tags/${LLAMA_CPP_TAG}.tar.gz)
  FetchContent_MakeAvailable(llama_cpp)
endif()

if(NOT ANDROID)
  find_package(JNI REQUIRED)
endif()
add_library(voiceassist_llama SHARED voiceassist_llama.cpp)
target_link_libraries(voiceassist_llama PRIVATE llama)
if(ANDROID)
  target_link_libraries(voiceassist_llama PRIVATE log)
  target_link_options(voiceassist_llama PRIVATE "-Wl,-z,max-page-size=16384")   # 16 KB page devices
else()
  target_include_directories(voiceassist_llama PRIVATE ${JNI_INCLUDE_DIRS})
endif()
