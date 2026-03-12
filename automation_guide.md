# Automating Wiktionary Page Creation from Google Sheets

Yes, it is absolutely possible to take a Google Sheet with Interslavic word translations and automatically create separate articles on your Interslavic Wiktionary. 

This is highly recommended for bootstrapping a wiki with a large amount of structured data.

## How it works:

The most standard and robust way to achieve this is using **Pywikibot**, which is a Python library developed specifically for the Wikimedia Foundation and MediaWiki wikis (like your Wiktionary). 

Here is what the workflow looks like:
1. **Export/Read Data**: Download the Google Sheet as a CSV, or use the Google Sheets API in Python to read the rows directly.
2. **Template generation**: Write a Python script that goes row by row. For each row (e.g., word: "slovo", en: "word", pos: "noun"), it interpolates the data into a standard Wikitext article string.
3. **Bot upload**: The script uses Pywikibot to log into your Interslavic Wiktionary via an API (using a bot account you create) and automatically publishes or updates the pages.

## Example Python logic:

```python
import pywikibot
import pandas as pd

# 1. Connect to your wiki
site = pywikibot.Site('interslavic', 'wiktionary') # We would configure this for your specific domain

# 2. Read the google table (e.g. exported as CSV)
df = pd.read_csv('interslavic_words.csv')

# 3. Loop and create pages
for index, row in df.iterrows():
    isv_word = row['isv_word']
    en_translation = row['en_translation']
    part_of_speech = row['pos']
    
    # Generate the Wikitext for the article
    page_content = f"""== Medžuslovjansky ==
=== Etimologija ===
...

=== {part_of_speech.capitalize()} ===
'''{isv_word}'''
# {en_translation}
"""
    
    # Select the page
    page = pywikibot.Page(site, isv_word)
    
    # Save the page with a bot summary
    if not page.exists():
        page.text = page_content
        page.save(u"Bot: Created new word entry from table")
```

## Next Steps
If you'd like to proceed with this:
1. We can write the Python script together.
2. You will need to make sure your Wiktionary allows API access (which it does by default if it's MediaWiki) and create a Bot Password for your user account.
3. We will need to design the exact Wikitext template you want each word's page to look like. (e.g. headers, categories, translation tables). 

Let me know if you want me to start working on a script to read your table!
