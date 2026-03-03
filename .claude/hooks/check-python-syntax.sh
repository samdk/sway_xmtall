#!/bin/bash
FILE_PATH=$(jq -r '.tool_input.file_path // empty')

# Only check .py files
[[ "$FILE_PATH" =~ \.py$ ]] || exit 0

OUTPUT=$(python3 -m py_compile "$FILE_PATH" 2>&1)
if [ $? -ne 0 ]; then
  echo "Python syntax error in $FILE_PATH:" >&2
  echo "$OUTPUT" >&2
  exit 2
fi
