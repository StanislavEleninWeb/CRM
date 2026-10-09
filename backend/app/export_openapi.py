"""Write the OpenAPI document to stdout. Used to generate the TypeScript client."""

import json
import sys

from app.main import create_app

if __name__ == "__main__":
    json.dump(create_app().openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
