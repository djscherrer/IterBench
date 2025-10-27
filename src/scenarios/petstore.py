import pathlib
from scenarios.base import Scenario

# Read the Petstore OpenAPI spec from the JSON file
with open("./src/scenario_files/petstore/petstore_api_spec.json", "r") as f:
    _JSON_API_SPEC = f.read()

SCENARIO = Scenario(
    id="Petstore",
    api_spec="",
    text_spec="",
    json_api_spec=_JSON_API_SPEC,
    short_app_description="Petstore API for managing pets, orders, and users",
    scenario_instructions="",
    needs_db=True,
    needs_secret=False,
    functional_tests=[],  # No tests for now
    security_tests=[],    # No tests for now
)
