import sys
import json
import os
import time
import logging
import re
from HeuristicBidding import compute_bid
#################################
# Claude OpenAI imports 
from anthropic import AnthropicVertex
from google.oauth2 import service_account
#################################
# Model Setup
CLAUDE_LOCATION   = os.getenv("CLAUDE_LOCATION")
CLAUDE_PROJECT_ID = os.getenv("CLAUDE_PROJECT_ID")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL")
TEMP = 0
MAX_TOKENS = 5000
#################################
# Logs - write to both console and file for visibility in subprocess
os.makedirs("/workspaces/SWARM-DMAS-wrench/logs", exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("/workspaces/SWARM-DMAS-wrench/logs/claude_bidder.log"),
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger(__name__)
#################################

def main():
    response = ""
    try:
        input_data = sys.stdin.read()
        data = json.loads(input_data)

        job_description = data["job_description"]
        system_description = data["hpc_system_description"]
        system_status = data["hpc_system_status"]
        runtime_prompt = data.get("prompt")

        # Start timing
        start_time = time.perf_counter()

        # Do not modify before here
        # Step1: Prompt instructions
        if not isinstance(runtime_prompt, str) or not runtime_prompt.strip():
            raise ValueError("No prompt provided. Set 'bidder_prompt_file' in the experiment config.")

        prompt = (
            runtime_prompt
            .replace("{job_description}", json.dumps(job_description, indent=2))
            .replace("{system_description}", json.dumps(system_description, indent=2))
            .replace("{system_status}", json.dumps(system_status, indent=2))
        )
        
        # Log prompt for debugging
        logger.debug(f"Prompt:\n{prompt}")
        # print(f"CLAUDE_PROMPT:\n{prompt}", file=sys.stderr, flush=True)

        # Step2: Format prompt
        message = [{"role": "user", "content": prompt}]

        # Step3: Setup Openai client
        client = AnthropicVertex(region=CLAUDE_LOCATION, project_id=CLAUDE_PROJECT_ID)

        # Step4: Get completion
        response = client.messages.create(
            model=CLAUDE_MODEL,
            messages=message,
            max_tokens=MAX_TOKENS,
            temperature=TEMP,
        )
        for content_block in response.content:
            response = content_block.text
        # Log response for debugging
        logger.debug(f"Response:\n{response}")
        # print(f"CLAUDE_RESPONSE:\n{response}", file=sys.stderr, flush=True)

    except Exception as e:
        print(json.dumps({"error" : str(e)}))
        logger.error(f"Claude bidder request failed: {e}")

    # End timing
    end_time = time.perf_counter()
    elapsed_time = end_time - start_time

    # Step5: Response Parsing
    bid_score_pattern = r'"bid_score":\s*([0-9]*\.?[0-9]+)'
    match = re.search(bid_score_pattern, response)

    # Step6: Heuristic bid if parsing fails
    # heuristic_bid = 0.1  # Change later with the heuristic logic as needed!  
    heuristic_bid = compute_bid(job_description, system_description, system_status)

    bid = float(match.group(1)) if match else heuristic_bid

    # Do not modify after here
    result = {
      "bid": bid,
      "bid_generation_time_seconds": round(elapsed_time, 6)
    }
    logger.info(json.dumps(result))
    print(json.dumps(result))

if __name__ == "__main__":
    main()
