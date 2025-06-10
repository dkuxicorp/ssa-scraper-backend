import logging
import azure.functions as func
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re
import json
import asyncio

# Define the Function App object. This is the central point for registering functions.
# We set the default authorization level to 'FUNCTION', requiring an API key.
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

def parse_age_string(age_str: str) -> float | None:
    """
    Parses complex age strings like "62 and 4 months" or "70b" into a float.
    This helper function is crucial for converting the table's text into usable numbers.
    """
    # Clean footnote characters ('a', 'b', etc.) from the string.
    cleaned_str = re.sub(r'[a-zA-Z]', '', age_str).strip()
    
    # Find all numbers in the cleaned string.
    parts = re.findall(r'(\d+\.?\d*)', cleaned_str)
    
    if len(parts) == 2:  # Format: "X and Y months"
        return float(parts[0]) + float(parts[1]) / 12.0
    elif len(parts) == 1:  # Format: "X"
        return float(parts[0])
    return None

# Register the function with a route. This function will now be triggered
# by HTTP requests to /api/scrape.
@app.route(route="scrape")
def scrape(req: func.HttpRequest) -> func.HttpResponse:
    """
    Main Azure Function entry point. This function receives the HTTP request,
    launches a headless browser to scrape the SSA website, parses the data,
    and returns it as a structured JSON object.
    """
    logging.info('Python HTTP trigger function processed a request for SSA scraping.')

    # --- 1. Get and Validate Input ---
    try:
        req_body = req.get_json()
        month_from_react = str(req_body.get('month'))
        day = str(req_body.get('day'))
        year = str(req_body.get('year'))
        sex = str(req_body.get('sex'))  # Expects 'm' or 'f'
    except (ValueError, AttributeError):
        return func.HttpResponse(
            body=json.dumps({"message": "Invalid request body. Please pass JSON with month, day, year, and sex."}),
            status_code=400,
            mimetype="application/json"
        )

    if not all([month_from_react, day, year, sex]):
        return func.HttpResponse(
            body=json.dumps({"message": "Missing one or more required parameters: month, day, year, sex."}),
            status_code=400,
            mimetype="application/json"
        )
        
    month_for_ssa = str(int(month_from_react) - 1)

    # --- 2. Perform Headless Browser Scraping ---
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page()
            page.goto("https://www.ssa.gov/cgi-bin/longevity.cgi", timeout=60000)
            
            logging.info("Page loaded. Locating form elements.")

            page.wait_for_selector('select[name="sex"]', timeout=30000)
            page.select_option('select[name="sex"]', sex)
            logging.info(f"Selected sex: {sex}")

            page.wait_for_selector('select[name="monthofbirth"]', timeout=30000)
            page.select_option('select[name="monthofbirth"]', month_for_ssa)
            logging.info(f"Selected month: {month_for_ssa}")
            
            page.wait_for_timeout(500) 

            page.wait_for_selector('select[name="dayofbirth"]', timeout=30000)
            page.select_option('select[name="dayofbirth"]', day)
            logging.info(f"Selected day: {day}")

            page.wait_for_selector('select[name="yearofbirth"]', timeout=30000)
            page.select_option('select[name="yearofbirth"]', year)
            logging.info(f"Selected year: {year}")

            logging.info("Form filled. Clicking submit.")
            
            page.click('input[type="submit"][value="Submit"]')
            page.wait_for_load_state('networkidle', timeout=60000)
            
            logging.info("Result page loaded. Parsing content.")
            content = page.content()
            browser.close()

        # --- 3. Parse the HTML Response ---
        soup = BeautifulSoup(content, 'html.parser')
        
        # --- FIX: More robust table finding logic ---
        # Instead of looking for a generic div, we look for the specific table
        # that contains the text "At Age", which is unique to the results table.
        all_tables = soup.find_all('table')
        results_table = None
        for table in all_tables:
            if "At Age" in table.get_text():
                results_table = table
                break

        if not results_table:
            raise ValueError("Could not find the results table on the SSA page. The page structure may have changed.")

        # --- 4. Structure the Scraped Data ---
        data = {"initial": {}, "future": []}
        rows = results_table.find_all('tr')
        is_first_row = True
        for row in rows[1:]:
            cols = [col.text.strip() for col in row.find_all('td')]
            if len(cols) == 3:
                age_str, additional_le_str, total_le_str = cols
                parsed_age = parse_age_string(age_str)
                if parsed_age is None:
                    continue
                point = {
                    "atAge": parsed_age,
                    "additionalLE": float(additional_le_str),
                    "totalLE": float(total_le_str)
                }
                if is_first_row:
                    data["initial"] = point
                    is_first_row = False
                else:
                    data["future"].append(point)
        
        if not data.get("initial"):
             raise ValueError("Failed to parse initial life expectancy data from the table.")

        # --- 5. Return Successful Response ---
        return func.HttpResponse(
            body=json.dumps(data),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        # Catch any exceptions during Playwright execution or parsing
        logging.error(f"An error occurred during scraping or parsing: {e}")
        return func.HttpResponse(
             body=json.dumps({"message": f"An internal error occurred: {str(e)}"}),
             mimetype="application/json",
             status_code=500
        )
