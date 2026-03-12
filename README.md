# Interslavic Wiktionary Bot

This repository contains the automation scripts for importing the official Interslavic dictionary (Medžuslovjansky Vikislovnik) into the [Wikimedia Incubator](https://incubator.wikimedia.org/wiki/Wp/isv/Main_Page).

The script automatically fetches the live Google Spreadsheet containing the official Interslavic vocabulary, processes it, generates full Wikitext grammar tables (declensions and conjugations), generates translations, and publishes the pages directly via the MediaWiki API.

## Project Structure

- `create_pages.py`: The main Python script that handles API communication, dictionary parsing, Wikitext layout, and automated uploads.
- `generate_tables.js`: A Node.js bridge script that utilizes `@interslavic/utils` to generate morphologically accurate grammatical forms and strips etymological spelling.
- `requirements.txt`: Python package dependencies.
- `package.json`: Node.js package dependencies.
- `automation_guide.md`: Historical working notes on the project's setup and MediaWiki configuration.

## Requirements

You need both Python and Node.js installed to run this project.

1. **Python 3.8+**
2. **Node.js 16+**

## Setup

1. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Node.js dependencies:**
   ```bash
   npm install
   ```

3. **Configure Environment Variables:**
   Copy the example environment file and fill in your MediaWiki bot credentials.
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and add your username and password:
   ```env
   WIKI_USERNAME="YourUsername@YourBotName"
   WIKI_PASSWORD="your_bot_password_here"
   ```

## Usage

Run the main Python script to start the page creation process. 

### Basic Usage
To run the bot in safely chunked batches (fetching from the live Google Sheet):
```bash
python3 create_pages.py
```

### Advanced Flags
- `--dry-run`: Do everything locally (download, generate Wikitext, print to output) but **skip** actually uploading to the Wiki. Great for testing formatting changes.
- `--limit [number]`: Only process a specific number of words. Example: `--limit 20`.
- `--start-from [row_index]`: Resume the script from a specific row number in the spreadsheet. Useful if the script was interrupted. Example: `--start-from 15000`.
- `--overwrite`: By default, the bot skips words that already have a page on the Wiki to prevent wiping out manual edits. Use this flag to force the bot to overwrite existing pages.

## How it Works

1. **Fetching**: The script connects directly to the published CSV format of the official Interslavic Google Sheet to get the most up-to-date words and translations.
2. **Grammar Processing**: `create_pages.py` pipes the word identifiers into `generate_tables.js`. This JS script utilizes the official `@interslavic/utils` library to morphologically generate all nouns, verbs, pronouns, and adjectives, then aggressively strips etymological letters out of the output to match standard Interslavic orthography.
3. **Wikitext Generation**: The script maps the grammatical outputs into massive, intricately styled HTML/Wikitext arrays, applying the `=== Spreženje ===` and `=== Sklonjenje ===` sections.
4. **Resiliency**: The bot is equipped with a `urllib3` retry adapter to silently survive transient 5xx server errors and reset connections. It intentionally waits between edits to respect Wikimedia's rate limits.
