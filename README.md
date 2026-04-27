# MITRE Caldera Plugin: Builder

## Overview:

The Builder plugin enables Caldera to dynamically compile code segments into payloads that can be executed as abilities 
by implants.

### Context:
Payload generation / infrastructure

### Known Limitations:
Currently, only C#, C, C++, and Golang are supported
- `csharp`: Compile C# executable using Mono
- `cpp_windows_x64`: Compile 64-bit Windows C++ executable using MXE/MinGW-w64
- `cpp_windows_x86`: Compile 64-bit Windows C++ executable using MXE/MinGW-w64
- `c_windows_x64`: Compile 64-bit Windows C executable using MXE/MinGW-w64
- `c_windows_x86`: Compile 64-bit Windows C executable using MXE/MinGW-w64
- `go_windows`: Build Golang executable for Windows

## Installation:

Install the required docker components with the following command:
```Bash
sudo ./install.sh
```

## Dependencies/Requirements:

1. Docker
2. docker-py
3. Go compiler

## Getting Started:

Here is a preview of a sample ability:
The following ability will compile the HelloWorld.exe executable, copy it to the machine running the agent, and execute
it using either cmd or PowerShell.

```yaml
---

- id: 096a4e60-e761-4c16-891a-3dc4eff02e74
  name: C# Hello World
  description: Dynamically compile HelloWorld.exe
  tactic: execution
  technique:
    attack_id: T1059
    name: Command-Line Interface
  platforms:
    windows:
      psh,cmd:
        build_target: HelloWorld.exe
        language: csharp
        code: |
          using System;

          namespace HelloWorld
          {
              class Program
              {
                  static void Main(string[] args)
                  {
                      Console.WriteLine("Hello World!");
                  }
              }
          }
```

DLL dependencies can be added by declaring a `payloads` list at the root of the ability.

