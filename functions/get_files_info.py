import os


def get_files_info(working_directory: str, directory: str = ".") -> str:
    try:
        working_dir_abs = os.path.abspath(working_directory)
        target_dir = os.path.normpath(os.path.join(working_dir_abs, directory))

        # Boolean check to see if target_dir falls within the absolute path of working_directory
        valid_target_dir = (
            os.path.commonpath([working_dir_abs, target_dir]) == working_dir_abs
        )

        # Guardrails
        if not valid_target_dir:
            return f'Error: Cannot list "{directory}" as it is outside the permitted working directory"'

        if not os.path.isdir(target_dir):
            return f'Error: "{directory}" is not a directory'

        # We can assume here, that path validation succeeded.

        lines = []

        if directory == ".":
            lines.append("Result for current directory:")
        else:
            lines.append(f"Result for '{directory}' directory:")

        for item in os.listdir(target_dir):
            item_path = os.path.join(target_dir, item)
            is_dir = os.path.isdir(item_path)  # Boolean
            file_size = os.path.getsize(item_path)
            lines.append(f"  - {item}: file_size={file_size} bytes, is_dir={is_dir}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"
