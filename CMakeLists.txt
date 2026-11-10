cmake_minimum_required(VERSION 3.28...3.30)

include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/common/bootstrap.cmake" NO_POLICY_SCOPE)

project(${_name} VERSION ${_version})

# The "Avatar Live Control" dock needs both. They default to ON on Windows; the
# source itself works with either OFF (properties-only control path, spec §6).
if(WIN32)
  set(_avatar_dock_default ON)
else()
  set(_avatar_dock_default OFF)
endif()
option(ENABLE_FRONTEND_API "Use obs-frontend-api for UI functionality" ${_avatar_dock_default})
option(ENABLE_QT "Use Qt functionality" ${_avatar_dock_default})

include(compilerconfig)
include(defaults)
include(helpers)

add_library(${CMAKE_PROJECT_NAME} MODULE)

find_package(libobs REQUIRED)
target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE OBS::libobs)

if(ENABLE_FRONTEND_API)
  find_package(obs-frontend-api REQUIRED)
  target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE OBS::obs-frontend-api)
endif()

if(ENABLE_QT)
  find_package(Qt6 COMPONENTS Widgets Core)
  target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE Qt6::Core Qt6::Widgets)
  target_compile_options(
    ${CMAKE_PROJECT_NAME}
    PRIVATE $<$<C_COMPILER_ID:Clang,AppleClang>:-Wno-quoted-include-in-framework-header -Wno-comma>
  )
  set_target_properties(
    ${CMAKE_PROJECT_NAME}
    PROPERTIES AUTOMOC ON AUTOUIC ON AUTORCC ON
  )
endif()

set_target_properties(${CMAKE_PROJECT_NAME} PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE plugin/include)

# libobs-independent core (also built standalone by tests/native).
target_sources(
  ${CMAKE_PROJECT_NAME}
  PRIVATE
    plugin/src/platform_clock.cpp
    plugin/src/frame_ring_reader.cpp
    plugin/src/ipc_client.cpp
    plugin/src/engine_launcher.cpp
    plugin/src/engine_controller.cpp
)

# OBS integration layer.
target_sources(
  ${CMAKE_PROJECT_NAME}
  PRIVATE plugin/src/module.cpp plugin/src/avatar_source.cpp plugin/src/avatar_render.cpp plugin/src/source_registry.cpp
)

if(ENABLE_QT AND ENABLE_FRONTEND_API)
  target_sources(${CMAKE_PROJECT_NAME} PRIVATE plugin/src/avatar_dock.cpp)
  target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE AVATAR_LIVE_ENABLE_DOCK)
endif()

if(WIN32)
  target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE bcrypt)
  target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
elseif(UNIX AND NOT APPLE)
  target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE rt)
endif()

set_target_properties_plugin(${CMAKE_PROJECT_NAME} PROPERTIES OUTPUT_NAME ${_name})
