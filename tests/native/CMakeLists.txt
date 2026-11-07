# Standalone build of the libobs-independent plugin core (IPC, frame ring,
# launcher, controller) plus the interop self-test driven by tests/integration.
#
#   cmake -S tests/native -B build_native && cmake --build build_native
cmake_minimum_required(VERSION 3.16)
project(avatar_ipc_selftest LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(PLUGIN_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../plugin")

add_library(
  avatar_core
  STATIC
  ${PLUGIN_DIR}/src/platform_clock.cpp
  ${PLUGIN_DIR}/src/frame_ring_reader.cpp
  ${PLUGIN_DIR}/src/ipc_client.cpp
  ${PLUGIN_DIR}/src/engine_launcher.cpp
  ${PLUGIN_DIR}/src/engine_controller.cpp
)
target_include_directories(avatar_core PUBLIC ${PLUGIN_DIR}/include)
find_package(Threads REQUIRED)
target_link_libraries(avatar_core PUBLIC Threads::Threads)
if(WIN32)
  target_link_libraries(avatar_core PUBLIC bcrypt)
  target_compile_definitions(avatar_core PUBLIC UNICODE _UNICODE NOMINMAX)
elseif(UNIX AND NOT APPLE)
  target_link_libraries(avatar_core PUBLIC rt)
endif()

if(MSVC)
  target_compile_options(avatar_core PRIVATE /W4)
else()
  target_compile_options(avatar_core PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(avatar_ipc_selftest selftest.cpp)
target_link_libraries(avatar_ipc_selftest PRIVATE avatar_core)
if(NOT MSVC)
  target_compile_options(avatar_ipc_selftest PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(avatar_core_unittest unittest.cpp)
target_link_libraries(avatar_core_unittest PRIVATE avatar_core)

enable_testing()
add_test(NAME avatar_core_unittest COMMAND avatar_core_unittest)
