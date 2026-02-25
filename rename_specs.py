import os
import sys

def replace_in_file(filepath):
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return

    new_content = content.replace("Red Hat", "MacOS")
    new_content = new_content.replace("RedHat", "MacOS")
    new_content = new_content.replace("redhat", "macos")
    new_content = new_content.replace("Redhat", "Macos")

    if new_content != content:
        try:
            with open(filepath, 'w') as f:
                f.write(new_content)
            print(f"Updated content in {filepath}")
        except Exception as e:
            print(f"Error writing to {filepath}: {e}")

def rename_files_and_directories(root_dir):
    # Rename files first
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if "redhat" in filename.lower():
                old_path = os.path.join(dirpath, filename)
                new_filename = filename.replace("redhat", "macos").replace("Redhat", "Macos").replace("RedHat", "MacOS")
                new_path = os.path.join(dirpath, new_filename)
                os.rename(old_path, new_path)
                print(f"Renamed file: {old_path} -> {new_path}")
                replace_in_file(new_path)
            else:
                replace_in_file(os.path.join(dirpath, filename))

    # Rename directories bottom-up
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
        for dirname in dirnames:
            if "redhat" in dirname.lower():
                old_path = os.path.join(dirpath, dirname)
                new_dirname = dirname.replace("redhat", "macos").replace("Redhat", "Macos").replace("RedHat", "MacOS")
                new_path = os.path.join(dirpath, new_dirname)
                os.rename(old_path, new_path)
                print(f"Renamed directory: {old_path} -> {new_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <target_directory>")
        sys.exit(1)
    
    target_dir = sys.argv[1]
    rename_files_and_directories(target_dir)
