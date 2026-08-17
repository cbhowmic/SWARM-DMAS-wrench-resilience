import math
import sys
import json
import time
import random
import hashlib

RESOURCE_COMPATIBILITY_TABLE = {

    # ================= HPC JOB =================
    ("HPC", True,  "HPC", True):   (1.00, 1.00),
    ("HPC", True,  "AI", True):    (0.90, 0.95),
    ("HPC", True,  "HYB", True):   (0.90, 0.95),
    ("HPC", True,  "HYB", False):  (0.00, 0.00),
    ("HPC", True,  "STO", False):  (0.00, 0.00),

    ("HPC", False, "HPC", True):   (0.80, 0.90),
    ("HPC", False, "AI", True):    (0.80, 0.90),
    ("HPC", False, "HYB", True):   (0.80, 0.90),
    ("HPC", False, "HYB", False):  (0.90, 0.95),
    ("HPC", False, "STO", False):  (0.70, 0.80),

    # ================= AI JOB =================
    ("AI", True,   "HPC", True):   (0.90, 0.95),
    ("AI", True,   "AI", True):    (1.00, 1.00),
    ("AI", True,   "HYB", True):   (0.90, 0.95),
    ("AI", True,   "HYB", False):  (0.00, 0.00),
    ("AI", True,   "STO", False):  (0.00, 0.00),

    ("AI", False,  "HPC", True):   (0.80, 0.90),
    ("AI", False,  "AI", True):    (0.80, 0.90),
    ("AI", False,  "HYB", True):   (0.80, 0.90),
    ("AI", False,  "HYB", False):  (0.90, 0.95),
    ("AI", False,  "STO", False):  (0.70, 0.80),

    # ================= HYB JOB =================
    ("HYB", True,  "HPC", True):   (0.90, 0.95),
    ("HYB", True,  "AI", True):    (0.90, 0.95),
    ("HYB", True,  "HYB", True):   (1.00, 1.00),
    ("HYB", True,  "HYB", False):  (0.00, 0.00),
    ("HYB", True,  "STO", False):  (0.00, 0.00),

    ("HYB", False, "HPC", True):   (0.80, 0.90),
    ("HYB", False, "AI", True):    (0.80, 0.90),
    ("HYB", False, "HYB", True):   (0.80, 0.90),
    ("HYB", False, "HYB", False):  (1.00, 1.00),
    ("HYB", False, "STO", False):  (0.75, 0.85),

    # ================= STO JOB =================
    ("STO", False, "HPC", True):   (0.70, 0.80),
    ("STO", False, "AI", True):    (0.70, 0.80),
    ("STO", False, "HYB", True):   (0.70, 0.80),
    ("STO", False, "HYB", False):  (0.90, 0.95),
    ("STO", False, "STO", False):  (1.00, 1.00),
}

def compute_bid(job_description, system_description, system_status):
    """Compute a heuristic bid score for a job on a given system_description.
    
    Args:
        job (dict): JobDescription "job_id", "user_id","group_id", "job_type",
            "submission_time", "walltime", "num_nodes", "needs_gpu", "requested_memory_gb", "requested_storage_gb",
            "hpc_site", "hpc_system"
        hpc_system (dict): HPC System description: "name", "type", "num_nodes",
            "memory_amount_in_gb", "storage_amount_in_gb", "has_gpu", "network_interconnect"
        system_status (dict): HPC System status: "current_num_available_nodes", "current_job_start_time_estimate", "queue_length"
        current_time (int): Current time in the scheduling system for wait time calculations.
    Returns:
        float 0.0 to 1.0.
    """
    # Job details
    nodes_req = job_description.get("num_nodes")
    req_gpu = job_description.get("needs_gpu")
    req_mem = job_description.get("requested_memory_gb")
    req_storage = job_description.get("requested_storage_gb")
    req_walltime = job_description.get("walltime") 
    job_type = job_description.get("job_type")
    job_site = job_description.get("hpc_site") 
    job_submission_time = job_description.get("submission_time") # in hours

    # System configuration
    sys_nodes = system_description.get("num_nodes")
    sys_has_gpu = system_description.get("has_gpu")
    sys_name = system_description.get("name")
    sys_type = system_description.get("type")
    sys_site = system_description.get("site") 
    sys_speed = system_description.get("node_speed") # in TFLOPS
    base_sys_speed = 1.5e12 
    sys_perf = round(sys_speed / base_sys_speed, 2)
    
    # System status
    sys_avail_nodes = system_status.get("current_num_available_nodes") 
    # Get a estimated start time
    current_job_start_time_estimate = system_status.get("current_job_start_time_estimate") # in seconds

    # --- 1. Feasibility ---
    # Note: Adding a check for storage capacity if the system defines it
    sys_total_storage = system_description.get("storage_amount_in_gb", float('inf'))
    
    if (nodes_req > sys_nodes):
        return 0.0
    if (req_gpu and not sys_has_gpu):
        return 0.0
    if (req_mem > system_description["memory_amount_in_gb"] * system_description["num_nodes"]): 
        return 0.0
    if (req_storage > sys_total_storage):
        return 0.0
        
    # --- 2. Utilization Score (Preference for availability) ---
    # Goal: Prefer systems that aren't hammered, but don't kill busy systems if they are fast.
    used_nodes = sys_nodes - sys_avail_nodes
    node_util = used_nodes / max(1.0, float(sys_nodes))
    score_util = 1.0 - node_util 
    
    # --- 3. Resource Compatibility (Type Matching) ---
    # Goal: Prefer systems that are a good match for the job type and requirements.
    lookup_key = (job_type, req_gpu, sys_type, sys_has_gpu)
    
    if lookup_key in RESOURCE_COMPATIBILITY_TABLE:
        min_score, max_score = RESOURCE_COMPATIBILITY_TABLE[lookup_key]
        
        # Create deterministic seed from job_id and system_type
        job_id_str = str(job_description.get("job_id"))
        seed_string = f"{job_id_str}_{sys_type}_{job_type}_{req_gpu}_{sys_has_gpu}"
        seed_value = int(hashlib.md5(seed_string.encode()).hexdigest()[:8], 16)
        random.seed(seed_value)
        score_resource = random.uniform(min_score, max_score)
    else:
        # Fallback for unexpected combinations
        score_resource = 0.5    # Neutral compatibility, if new job types appear
    
    # --- 4. Time Cost Calculation ---
    # A. Queue Wait Time
    r_j = job_submission_time  # in seconds
    wait_time = current_job_start_time_estimate - r_j# in seconds
    
    # B. Execution Time (adjusted for hardware speed)
    pred_exec_time = (req_walltime) / sys_perf
    
    total_time_cost = wait_time + pred_exec_time
    # total_time_cost = C_j -  r_j # in seconds
    
    slowdown = max(0.0, total_time_cost) / max(pred_exec_time , 1.0) # both in seconds
    alpha = 0.1
    norm_slowdown = math.exp(-alpha * slowdown) # lower slowdown => higher score
   
    # --- 5. Weighted Aggregation ---
    # Define importance of each factor
    w_util = 0.6      # Change to Low weight: Don't worry too much if system is busy ~ 0.1
    w_resource = 0.1  # Change to Medium: Prefer correct hardware types ~ 0.3
    w_speed = 0.3    # Change to High: User cares most about "When is my job done?" ~ 0.6
    
    # --- 6. AI data-transfer penalty on final score (10-20%) ---
    ai_data_xfer_penalty = 0.0
    if job_type == "AI" and job_site and sys_site and (job_site != sys_site):
        seed_string = f"{job_description.get('job_id')}_{sys_name}_ai_xfer"
        seed_value = int(hashlib.md5(seed_string.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed_value)
        ai_data_xfer_penalty = rng.uniform(0.10, 0.20)
    
    # Normalization
    # final_score = (
    #     (score_util * w_util) + 
    #     (score_resource * w_resource) + 
    #     ((norm_slowdown - ai_data_xfer_penalty) * w_speed) 
    # ) / (w_util + w_resource + w_speed)
    final_score = (
        (score_util) + 
        (score_resource) + 
        ((norm_slowdown - ai_data_xfer_penalty)) 
    ) / (3.0)
    
    return round(final_score, 4)

def main():
    try:
        input_data = sys.stdin.read()
        data = json.loads(input_data)

        job_description = data["job_description"]
        system_description = data["hpc_system_description"]
        system_status = data["hpc_system_status"]
        
        # Start timing
        start_time = time.perf_counter()

        # Do not modify before here
        # Add logic to generate a bid based on the job and system descriptions and system status here
        bid = compute_bid(job_description, system_description, system_status)

        # End timing
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time

        # Do not modify after here
        result = {
            "bid": bid,
            "bid_generation_time_seconds": round(elapsed_time, 6)
        }

        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e)}))

if __name__ == "__main__":
    main()