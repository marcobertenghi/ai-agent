import os
import subprocess

schema_run_python_file = {
    "type": "function",
    "function": {
        "name": "run_python_file",
        "description": "Executes a Python file within the working directory and returns its output.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the Python file to execute, relative to the working directory",
                },
                "args": {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                    "description": "Optional command-line arguments to pass to the Python script",
                },
            },
            "required": ["file_path"],
        },
    },
}


def run_python_file(
    working_directory: str, file_path: str, args: list[str] | None = None
) -> str:
    try:
        working_dir_abs = os.path.abspath(working_directory)
        target_path = os.path.normpath(os.path.join(working_dir_abs, file_path))

        valid_target_path = (
            os.path.commonpath([working_dir_abs, target_path]) == working_dir_abs
        )

        # Guardrails
        ## Check whether we are outside the working directory
        if not valid_target_path:
            return (
                f'Error: Cannot execute "{file_path}" '
                f"as it is outside the permitted working directory"
            )

        ## Check if target file path exists and points to a regular file
        if not os.path.isfile(target_path):
            return f'Error "{file_path}" does not exists or is not a regular file'

        ## Check if the file name does not end with .py
        if file_path[-3:] != ".py":
            return f'Error: "{file_path}" is not a Python file'

        ## Suppose all the above test have passed here.
        command = ["python", target_path]

        if args is not None:
            command.extend(args)

        # Create the subprocess
        command_run = subprocess.run(
            command,
            cwd=working_dir_abs,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        output_string = ""

        if command_run.returncode != 0:
            output_string += f"Process excited with code {command_run.returncode}"

        if command_run.stdout == "" and command_run.stderr == "":
            output_string += "No output produced"
        else:
            output_string += (
                f"STDOUT: {command_run.stdout} STDERR: {command_run.stderr}"
            )

        return output_string

    except Exception as e:
        return f"Error: executing Python file: {e}"
