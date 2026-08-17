import sys
import json
import time

# Reuse the compute_bid function from HeuristicBidding
from HeuristicBidding import compute_bid


def select_best_system(job_description, systems, current_simulated_time):
    """Select the best system for a job given all available systems.

    Args:
        job_description (dict): The job to schedule.
        systems (list): List of dicts, each containing "system_name", "description", and "status".
        current_simulated_time (float): Current simulation time.

    Returns:
        str or None: Name of the best system, or None if no system can run the job.
    """
    best_system = None
    best_bid = -1.0

    for system in systems:
        system_name = system["system_name"]
        description = system["description"]
        status = system["status"]

        bid = compute_bid(job_description, description, status, current_simulated_time)

        if bid > best_bid:
            best_bid = bid
            best_system = system_name

    # Return None if all bids were 0 (no feasible system)
    if best_bid <= 0:
        return None

    return best_system


def main():
    try:
        input_data = sys.stdin.read()
        data = json.loads(input_data)

        job_description = data["job_description"]
        systems = data["systems"]
        current_simulated_time = data.get("current_simulated_time", 0)

        # Start timing
        start_time = time.perf_counter()

        # Select the best system
        selected_system = select_best_system(job_description, systems, current_simulated_time)

        # End timing
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time

        result = {
            "selected_system": selected_system,
            "decision_time_seconds": round(elapsed_time, 6)
        }

        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e), "selected_system": None}))


if __name__ == "__main__":
    main()
