import os


def get_file_content(working_directory: str, file_path: str) -> str:
    try:
        working_dir_abs = os.path.abspath(working_directory)
        target_dir = os.path.normpath(os.path.join(working_dir_abs, file_path))

        # Boolean check to see if target_dir falls within the absolute path of working_directory
        valid_file_path = (
            os.path.commonpath([working_dir_abs, target_dir]) == working_dir_abs
        )

        # Guardrails
        ## Check if file path is valid
        if not valid_file_path:
            return f'Error: Cannot read "{file_path}" as it is outside the permitted working directory"'

        ## Check provided target directory is indeed a file
        if not os.path.isfile(target_dir):
            return f'Error: File not found or is not a regular file "{file_path}"'

        # Here we can assume that (almost) everything is alright

        MAX_CHARS: int = 10000
        with open(target_dir, mode="r") as file:
            content = file.read(MAX_CHARS)
            # Check for truncation
            if file.read(1):
                content += (
                    f'[... File "{file_path}" truncated at {MAX_CHARS} characters]'
                )
        return content
    except Exception as e:
        return f"Error: {e}"
