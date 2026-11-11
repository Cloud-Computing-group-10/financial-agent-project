# COMP4651: Cloud Computing and Big Data Systems
## Deep Project: Financial Research and Analysis Agent (Route D3)

A robust, traceable financial research assistant that parses SEC 10-Q quarterly filings, routes queries between deterministic SQL lookups and semantic search, and grounds every financial figure and explanation in its exact source document.

---

## 🚀 Setup & Installation

Follow these instructions to set up your local development environment and configure access to the HKUST AI Gateway.

### Prerequisites
* **Conda** (Anaconda or Miniconda) installed on your system.
* **Python 3.11+** (managed automatically via the setup script).
* A valid **HKUST ITSO Generative AI API Key**.

---

### Step 1: Clone the Repository
Clone the repository to your local machine and navigate into the project directory (ensure your remote URL is configured to SSH):

    git clone git@github.com:your-organization/your-repo-name.git
    cd your-repo-name

---

### Step 2: Run the Automated Setup Script
We provide an automated shell script (`setup.sh`) that sets up your Conda environment named `financial-agent-env`, installs all required runtime dependencies (`openai`, `python-dotenv`), and generates a local `.env` template.

Make the script executable and run it:

    chmod +x setup.sh
    ./setup.sh

---

### Step 3: Configure Your API Key
The setup script automatically generates a `.env` file in your root directory (which is safely ignored by Git). 

1. Open the `.env` file in your preferred text editor.
2. Insert your actual HKUST API key:

    HKUST_API_KEY=your_actual_api_key_here

---

### Step 4: Verify the Installation & Connection
Activate your Conda environment and run the baseline test script to verify that your connection to the HKUST Azure gateway is working properly:

    # 1. Activate the Conda environment
    conda activate financial-agent-env

    # 2. Run the baseline script
    python3 baseline.py

---

## 💻 How to Use

The core baseline program (`baseline.py`) is structured to accept raw SEC 10-Q filing text and financial questions, routing them through the `gpt-4o-mini` model via the HKUST API gateway while tracking latency and token usage metrics.

### Running the Baseline Script
To test the default baseline execution with built-in sample data:

    conda activate financial-agent-env
    python3 baseline.py

### Core Program Parameters
If you are importing or calling the baseline runner programmatically in your code, the main function accepts two primary parameters:
* **`filing_text`** *(str)*: The raw or cleaned text extracted from the SEC 10-Q filing.
* **`question`** *(str)*: The financial query you want the model to answer based strictly on the provided text.