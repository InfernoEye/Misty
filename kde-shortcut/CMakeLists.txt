cmake_minimum_required(VERSION 3.20)

project(misty-shortcut LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui)
find_package(KF6GlobalAccel REQUIRED)

add_executable(misty-shortcut main.cpp)

target_link_libraries(misty-shortcut
    PRIVATE
        Qt6::Core
        Qt6::Gui
        KF6::GlobalAccel
)

include(CTest)
if(BUILD_TESTING)
    add_executable(test-command-reader test_command_reader.cpp)
    target_link_libraries(test-command-reader PRIVATE Qt6::Core)
    add_test(NAME shortcut-command-reader COMMAND test-command-reader)
    set_tests_properties(shortcut-command-reader PROPERTIES TIMEOUT 5)
endif()
