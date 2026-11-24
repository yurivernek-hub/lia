# Jarvis B - background (actual desktop background, via layer-shell).
#
# Compilar (Arch):
#   sudo pacman -S --needed base-devel cmake qt6-base qt6-declarative layer-shell-qt
#   cd ~/jarvisb/background
#   cmake -B build -S .
#   cmake --build build -j
# The binary is placed in build/jarvisb-background (jarvisb.py launches it automatically).

cmake_minimum_required(VERSION 3.20)
project(jarvisb-background LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui Qml Quick Network DBus)
find_package(LayerShellQt REQUIRED)

add_executable(jarvisb-background main.cpp bridge.cpp bridge.h)

# background.qml is read directly from this directory (not packaged into the binary).
target_compile_definitions(jarvisb-background PRIVATE
    QML_DIR="${CMAKE_CURRENT_SOURCE_DIR}")

target_link_libraries(jarvisb-background PRIVATE
    Qt6::Core Qt6::Gui Qt6::Qml Qt6::Quick Qt6::Network Qt6::DBus
    LayerShellQt::Interface)