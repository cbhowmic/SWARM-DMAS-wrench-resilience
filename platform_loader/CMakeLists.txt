cmake_minimum_required(VERSION 3.16)
project(json_platform_loader LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Add local cmake modules
list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")

# Find required packages
if(NOT TARGET SimGrid::SimGrid)
    find_package(SimGrid REQUIRED)
endif()

if(NOT TARGET FSMOD::FSMOD)
    find_package(FSMod REQUIRED)
endif()

find_package(nlohmann_json REQUIRED)

# Main shared library: JSON-based platform loader
add_library(platform SHARED json_platform_loader.cpp)

target_include_directories(platform PRIVATE
    ${SimGrid_INCLUDE_DIR}
    ${FSMOD_INCLUDE_DIR}
)

target_link_libraries(platform PRIVATE
  SimGrid::SimGrid
  FSMOD::FSMOD
  nlohmann_json::nlohmann_json
  ${CMAKE_DL_LIBS}
)

# Platform summary utility (standalone, takes platform file as argument)
add_executable(platform_summary platform_summary.cpp)

target_link_libraries(platform_summary PRIVATE
  platform
  SimGrid::SimGrid
  FSMOD::FSMOD
)
target_include_directories(platform_summary PRIVATE
    ${SimGrid_INCLUDE_DIR}
    ${FSMOD_INCLUDE_DIR}
)


# Set the config path for the test
add_test(NAME cluster25_comparison COMMAND test_cluster25)
set_tests_properties(cluster25_comparison PROPERTIES
  ENVIRONMENT "PLATFORM_CONFIG=${CMAKE_CURRENT_SOURCE_DIR}/tests/platform_cluster25.json"
)

# Install rules
install(TARGETS platform LIBRARY DESTINATION lib)
install(FILES platform_config.json DESTINATION lib)
install(TARGETS platform_summary RUNTIME DESTINATION bin)

# Copy config files to build directory for convenience
configure_file(${CMAKE_CURRENT_SOURCE_DIR}/platform_config.json
               ${CMAKE_CURRENT_BINARY_DIR}/platform_config.json COPYONLY)
