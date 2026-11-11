import os
from dotenv import load_dotenv
from openai import OpenAI

# Load local environment variables from .env
load_dotenv()

def run_hkust_baseline(filing_text: str, question: str) -> str:
    api_key = os.getenv("HKUST_API_KEY")
    
    if not api_key or api_key == "your_actual_api_key_here":
        raise ValueError("HKUST_API_KEY is missing or unconfigured! Update your local .env file.")

    # Configure client pointing to HKUST's Azure API gateway endpoint
    client = OpenAI(
        base_url="https://hkust.azure-api.net/hkust-genai/v1",
        api_key=api_key,
        default_headers={"api-key": api_key} # Required custom header for HKUST APIM
    )

    prompt = f"""
You are a financial research assistant acting as a baseline evaluation model.
Answer the user's question using ONLY the provided SEC 10-Q filing text below.
Question: {question}

---
SEC 10-Q Filing Text:
{filing_text}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    
    return response.choices[0].message.content

if __name__ == "__main__":
    sample_text = "CONSOLIDATED STATEMENTS OF OPERATIONS... Net revenues: $12,450 million."
    sample_question = "What was the total net revenue?"
    
    print("Testing HKUST Cloud Baseline API...")
    answer = run_hkust_baseline(sample_text, sample_question)
    print("\nBaseline Response:\n", answer)