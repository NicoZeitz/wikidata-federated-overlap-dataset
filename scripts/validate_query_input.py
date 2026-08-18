import argparse
import json
from pathlib import Path

_DEFAULT_INPUT_DIR = Path(__file__).parent.parent / "input"


def run_script(args: argparse.Namespace) -> None:
    # go through each file inside the input directory (only .json)
    for file in args.input_dir.glob("*.json"):
        if args.verbose:
            print(f"Validating {file.stem}...")

        # load file with json
        try:
            with open(file) as f:
                data = json.load(f)
        except Exception:
            print(f"  ERROR: Validation failed for {file.stem}: Not valid json")
            continue

        relevant_agents: set[str] = set(data["extra"]["relevant_data_agents"])
        steps: dict[str, set[str]] = {
            step["id"]: (set(step["to_agent"]) if isinstance(step["to_agent"], list) else set([step["to_agent"]]))
            - {"Coordinator"}
            for step in data["extra"]["query_plan"]["steps"]
        }
        query_plan_relevant_agents: set[str] = set()
        for _step_id, to_agents in steps.items():
            query_plan_relevant_agents.update(to_agents)

        # compare relevant_agents and query_plan_relevant_agents
        if relevant_agents == query_plan_relevant_agents:
            if args.verbose:
                print("  PASSED")
            continue

        # mismatch
        print(f"  ERROR: Validation failed for {file.stem}")
        # find out steps that have agents that are not in relevant_agents
        for step_id, to_agents in steps.items():
            missing_agents_in_step = to_agents - relevant_agents
            if missing_agents_in_step:
                missing_agents_str = json.dumps(sorted(missing_agents_in_step))
                print(f"    Step {step_id} has agents not in relevant agents: {missing_agents_str}")

        # find out relevant agents that are not in query_plan_relevant_agents
        missing_agents = relevant_agents - query_plan_relevant_agents
        if missing_agents:
            print(f"    Relevant agents not in query plan: {json.dumps(sorted(missing_agents))}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the input folder that relevant agents are all used inside the query plan."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=_DEFAULT_INPUT_DIR,
        metavar="DIR",
        help="Input directory containing the JSON files to validate",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print verbose output during validation",
    )
    return parser.parse_args()


if __name__ == "__main__":
    run_script(_parse_args())
