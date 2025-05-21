import streamlit as st
from docx import Document
import pandas as pd
import json
import re
import io
import requests
from boxsdk import OAuth2, Client

st.set_page_config(page_title="Conga to Box Doc Gen", layout="centered")

# Box API configuration
def get_box_client():
    client_id = st.secrets["box"]["BOX_CLIENT_ID"]
    client_secret = st.secrets["box"]["BOX_CLIENT_SECRET"]
    developer_token = st.secrets["box"]["BOX_DEVELOPER_TOKEN"]
    
    oauth = OAuth2(
        client_id=client_id,
        client_secret=client_secret,
        access_token=developer_token,
    )
    client = Client(oauth)
    return client

def get_folder_contents(client, folder_id):
    folder = client.folder(folder_id).get(fields=['name', 'parent'])
    items = client.folder(folder_id).get_items()
    return folder, items

def navigate_folders(client, current_folder_id):
    folder, items = get_folder_contents(client, current_folder_id)
    
    path_parts = []
    ancestor_tracer = folder
    
    while True:
        if not hasattr(ancestor_tracer, 'parent') or not ancestor_tracer.parent:
            break 
        parent_ref = ancestor_tracer.parent
        if parent_ref.id == "0":
            break 
        try:
            parent_full_obj = client.folder(parent_ref.id).get(fields=["name", "parent"])
        except Exception as e:
            st.warning(f"Could not retrieve parent folder details for ID {parent_ref.id}: {e}")
            break
        path_parts.insert(0, {"id": parent_full_obj.id, "name": parent_full_obj.name})
        ancestor_tracer = parent_full_obj
    
    breadcrumb_cols = st.columns(len(path_parts) + 1) 
    breadcrumb_cols[0].button("Root", key="nav_root_button", 
                              on_click=lambda: st.session_state.update({"current_folder": "0"}))
    
    for i, part in enumerate(path_parts):
        breadcrumb_cols[i+1].button(
            part["name"], 
            key=f"nav_breadcrumb_{part['id']}", 
            on_click=lambda folder_id_val=part["id"]: st.session_state.update({"current_folder": folder_id_val})
        )
    
    st.subheader(f"Folder: {folder.name}")
    header_cols = st.columns([0.7, 0.2, 0.1])
    header_cols[0].write("Name")
    header_cols[1].write("Type")
    header_cols[2].write("Select")
        
    for item in items:
        item_cols = st.columns([0.7, 0.2, 0.1])
        if item.type == "folder":
            item_cols[0].write(f"📁 {item.name}")
            item_cols[1].write("Folder")
            item_cols[2].button("Open", key=f"open_folder_{item.id}", 
                                on_click=lambda folder_id_val=item.id: st.session_state.update({"current_folder": folder_id_val}))
        else:
            item_cols[0].write(f"📄 {item.name}")
            item_cols[1].write(item.type.capitalize())
            file_extension = item.name.split('.')[-1].lower() if '.' in item.name else ''
            
            if file_extension == 'docx':
                selected = item_cols[2].checkbox(" ", key=f"select_template_{item.id}", 
                                                  value=st.session_state.get('template_file_id') == item.id)
                if selected:
                    st.session_state['template_file_id'] = item.id; st.session_state['template_file_name'] = item.name; st.session_state['template_file_type'] = 'docx'
                elif st.session_state.get('template_file_id') == item.id:
                     st.session_state.pop('template_file_id', None); st.session_state.pop('template_file_name', None); st.session_state.pop('template_file_type', None)
            elif file_extension in ['txt', 'csv']:
                selected = item_cols[2].checkbox(" ", key=f"select_query_{item.id}", 
                                                  value=st.session_state.get('query_file_id') == item.id)
                if selected:
                    st.session_state['query_file_id'] = item.id; st.session_state['query_file_name'] = item.name; st.session_state['query_file_type'] = file_extension
                elif st.session_state.get('query_file_id') == item.id:
                     st.session_state.pop('query_file_id', None); st.session_state.pop('query_file_name', None); st.session_state.pop('query_file_type', None)
            elif file_extension == 'json':
                selected = item_cols[2].checkbox(" ", key=f"select_schema_{item.id}", 
                                                  value=st.session_state.get('schema_file_id') == item.id)
                if selected:
                    st.session_state['schema_file_id'] = item.id; st.session_state['schema_file_name'] = item.name; st.session_state['schema_file_type'] = 'json'
                elif st.session_state.get('schema_file_id') == item.id:
                     st.session_state.pop('schema_file_id', None); st.session_state.pop('schema_file_name', None); st.session_state.pop('schema_file_type', None)

def download_box_file(client, file_id):
    return client.file(file_id).content()

def extract_merge_fields(docx_content):
    with io.BytesIO(docx_content) as docx_bytes:
        document = Document(docx_bytes)
        fields = set()
        pattern = r"\{\{(.*?)(?:\||\}\}|\\@)" # Adjusted to better handle formatting like \@
        for para in document.paragraphs:
            matches = re.findall(pattern, para.text)
            for match in matches: fields.add(match[0].strip())
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para_in_cell in cell.paragraphs:
                        matches = re.findall(pattern, para_in_cell.text)
                        for match in matches: fields.add(match[0].strip())
        return list(fields)

def parse_conga_query(file_content, file_type):
    all_parsed_fields = set() 

    if file_type == 'csv':
        try:
            df = pd.read_csv(io.BytesIO(file_content), header=None)
            potential_query_column = None
            if not df.empty:
                first_cell_value = str(df.iloc[0, 0]).lower()
                # Check if the first row looks like a header of queries or actual queries
                if "select " in first_cell_value and " from " in first_cell_value:
                    potential_query_column = df.iloc[:, 0] # Process all rows in first col
                    st.info("CSV: Processing first column as SOQL queries, starting from first row.")
                elif len(df) > 1: # If more than one row, assume header + data
                    second_cell_value = str(df.iloc[1,0]).lower()
                    if "select " in second_cell_value and " from " in second_cell_value:
                         potential_query_column = df.iloc[1:, 0] # Skip header, process first col
                         st.info("CSV: Skipped header, processing first column as SOQL queries.")
                    else: # Fallback to original behavior: treat first row as headers
                        st.info("CSV: First column of data rows does not look like SOQL. Treating first row as column headers.")
                        df_with_header = pd.read_csv(io.BytesIO(file_content)) # Re-read with header
                        all_parsed_fields.update(list(df_with_header.columns))
                        return list(all_parsed_fields)
                else: # Single row CSV
                    if "select " in first_cell_value and " from " in first_cell_value: # is it a query?
                        potential_query_column = df.iloc[:,0]
                    else: # treat as header
                        st.info("CSV: Single row, treating as column headers.")
                        all_parsed_fields.update(list(df.columns)) # df was read with header=None, so columns are 0,1,2...
                        df_with_header = pd.read_csv(io.BytesIO(file_content)) # Re-read with header
                        all_parsed_fields.update(list(df_with_header.columns))
                        return list(all_parsed_fields)


            if potential_query_column is not None:
                num_processed = 0
                for idx, query_text_obj in potential_query_column.dropna().items():
                    query_text = str(query_text_obj)
                    if "select " in query_text.lower() and " from " in query_text.lower():
                        num_processed += 1
                        match = re.search(r"select\s+(.+?)\s+from", query_text, re.IGNORECASE | re.DOTALL)
                        if match:
                            fields_segment = match.group(1)
                            raw_fields = [f.strip() for f in fields_segment.split(",")]
                            all_parsed_fields.update([f for f in raw_fields if f])
                if num_processed > 0 and all_parsed_fields:
                     st.success(f"CSV: Extracted {len(all_parsed_fields)} unique fields from {num_processed} SOQL queries.")
                elif num_processed > 0 :
                     st.warning(f"CSV: Processed {num_processed} SOQL queries but extracted no fields.")
                else:
                     st.warning("CSV: No SOQL queries found to process in the expected column.")


        except Exception as e:
            st.error(f"Error parsing CSV query file: {e}. Trying to read as plain text SOQL.")
            try:
                text_content = file_content.decode("utf-8")
                return parse_conga_query(text_content.encode("utf-8"), 'txt')
            except Exception as e_txt:
                st.error(f"Fallback to TXT parsing also failed: {e_txt}")
                return []
    
    elif file_type == 'txt':  
        text = file_content.decode("utf-8")
        queries = re.split(r';\s*\n?|\n\s*(?=\S)', text) # Split by semicolon or newlines that precede content
        num_processed = 0
        for query_text in queries:
            query_text = query_text.strip()
            if not query_text: continue
            if "select " in query_text.lower() and " from " in query_text.lower():
                num_processed +=1
                match = re.search(r"select\s+(.+?)\s+from", query_text, re.IGNORECASE | re.DOTALL)
                if match:
                    fields_segment = match.group(1)
                    raw_fields = [f.strip() for f in fields_segment.split(",")] 
                    all_parsed_fields.update([f for f in raw_fields if f])
        if num_processed > 0 and all_parsed_fields:
             st.success(f"TXT: Extracted {len(all_parsed_fields)} unique fields from {num_processed} SOQL queries.")
        elif num_processed > 0:
             st.warning(f"TXT: Processed {num_processed} SOQL queries but extracted no fields.")
        else:
             st.warning("TXT: No SOQL queries found to process.")
    
    else: 
         st.error(f"Unsupported query file type: {file_type}")

    return list(all_parsed_fields)


def generate_prompt_single_call(merge_fields, conga_fields):
    merge_fields_str = ", ".join(merge_fields)
    conga_fields_str = ", ".join(conga_fields)
    
    prompt = (
        "You are an AI assistant helping map fields from a Conga document generation system to a Box Doc Gen system. "
        "You have been provided with a Salesforce schema file as context (see the 'items' passed to the API).\n\n"
        "Conga Template Merge Fields:\n"
        f"{merge_fields_str}\n\n"
        "Fields from Conga Query (likely Salesforce source fields from the main object or related objects mentioned in the schema file):\n"
        f"{conga_fields_str}\n\n"
        "Task: Using the provided Salesforce schema file (passed as an item to this API call), map each 'Conga Template Merge Field' to the most relevant field in that schema. "
        "The 'Fields from Conga Query' can provide hints about the original Salesforce source of the Conga fields.\n"
        "Respond ONLY in CSV format with three columns: CongaField,BoxField,FieldType.\n"
        "CongaField: The exact merge field name from the Conga template.\n"
        "BoxField: The corresponding full path from the Salesforce Schema file (e.g., Account.Name, Opportunity.LineItems.0.Product.Name). Refer to the provided schema file for these paths.\n"
        "FieldType: The data type of the BoxField if known from schema or inferable (e.g., Text, Number, Date, Boolean, RichText).\n"
        "If a direct match is not clear, use your best judgment or leave BoxField empty for that CongaField.\n"
        "Example Row: CongaAmountField,Opportunity.Amount,Currency"
    )
    return prompt

def call_box_ai(prompt, grounding_file_id, developer_token):
    url = "https://api.box.com/2.0/ai/text_gen"
    headers = {"Authorization": f"Bearer {developer_token}", "Content-Type": "application/json"}
    items_payload = []
    if grounding_file_id:
        items_payload.append({"type": "file", "id": str(grounding_file_id), "content_type": "text_content"})
    else: 
        st.error("CRITICAL: grounding_file_id missing for call_box_ai. API may fail.")
    
    data = {"prompt": prompt, "items": items_payload}
    
    st.info("Box AI Request Payload (see console/logs for full details):")
    st.json(data) 
    print(f"--- Sending Box AI Request ---\nURL: {url}\nHeaders: {json.dumps(headers, indent=2)}\nPayload: {json.dumps(data, indent=2)}\n-----------------------------")
    
    response = requests.post(url, headers=headers, json=data)
    
    print(f"--- Received Box AI Response ---\nStatus Code: {response.status_code}")
    response_text_for_return = None
    try: 
        response_data = response.json()
        print(f"Response Body: {json.dumps(response_data, indent=2)}")
        if response.status_code == 200:
            response_text_for_return = response_data.get("answer", "") 
    except json.JSONDecodeError: 
        print(f"Response Body (Non-JSON): {response.text}")
        if response.status_code == 200: 
            response_text_for_return = response.text 
    except Exception as e: 
        print(f"Could not process Box AI response for logging: {e}\nRaw: {response.text}")
    print("-----------------------------")

    if response.status_code == 200: 
        return response_text_for_return
    else: 
        st.error(f"Box AI request failed: {response.status_code} — {response.text}"); 
        return None

def convert_response_to_df(text):
    if not text or not isinstance(text, str):
        st.warning("AI response was empty or not a string.")
        return pd.DataFrame()

    csv_match = re.search(r"```csv\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if csv_match:
        csv_text = csv_match.group(1).strip()
        st.info("Extracted CSV block from AI response.")
    else:
        csv_text = text.strip()
        st.info("No explicit CSV markdown block (```csv...```) found, attempting to parse response as is or find header.")

    lines = csv_text.strip().splitlines()
    
    header_idx = -1
    expected_headers = ["CongaField", "BoxField", "FieldType"]
    for i, line in enumerate(lines):
        if all(expected_header in line for expected_header in expected_headers):
            header_idx = i
            break
    
    if header_idx == -1:
        st.warning(f"CSV header ({', '.join(expected_headers)}) not found in processed AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame()

    header_from_ai = [h.strip() for h in lines[header_idx].split(",")]
    data_lines = lines[header_idx+1:]
    data = []

    for line_num, line_content in enumerate(data_lines):
        line_content_stripped = line_content.strip()
        if not line_content_stripped: continue
        
        if not line_content_stripped[0].isalnum() and line_content_stripped[0] not in ['{', '"', '('] : 
            if len(line_content_stripped.split(",")) < 2 : 
                 st.info(f"Skipping likely non-data line: '{line_content_stripped}'")
                 continue

        split_line = [val.strip() for val in line_content_stripped.split(",")]

        if len(split_line) == len(header_from_ai):
            data.append(split_line)
        elif len(split_line) > len(header_from_ai) and len(header_from_ai) == 3:
            data.append([split_line[0], split_line[1], ",".join(split_line[2:])])
        elif len(split_line) == 2 and len(header_from_ai) == 3:
            data.append([split_line[0], split_line[1], ""])
            st.info(f"Line #{header_idx+2+line_num} had 2 columns, assuming missing FieldType: '{line_content_stripped}'")
        elif len(split_line) == 1 and len(header_from_ai) == 3 and split_line[0]:
            data.append([split_line[0], "", ""])
            st.info(f"Line #{header_idx+2+line_num} had 1 column, assuming only CongaField: '{line_content_stripped}'")
        elif len(split_line) > 0 :
             st.warning(f"Skipping malformed CSV line #{header_idx+2+line_num} (cols {len(split_line)} vs {len(header_from_ai)}): '{line_content_stripped}'")
    
    if not data:
        st.warning("No valid data rows extracted from AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame()
        
    return pd.DataFrame(data, columns=header_from_ai) if data else pd.DataFrame(columns=expected_headers)


if 'current_folder' not in st.session_state: st.session_state['current_folder'] = "0"
st.title("Conga Template to Box Doc Gen Mapper")
with st.sidebar:
    st.header("Box File Browser")
    try:
        client = get_box_client()
        navigate_folders(client, st.session_state['current_folder']) 
        st.subheader("Selected Files")
        st.write(f"Template: {st.session_state.get('template_file_name', 'Not selected')}")
        st.write(f"Query: {st.session_state.get('query_file_name', 'Not selected')}")
        st.write(f"Schema: {st.session_state.get('schema_file_name', 'Not selected')}")
    except Exception as e: st.error(f"Box connection/browsing error: {str(e)}"); client = None 

if st.button("Generate Field Mapping"):
    if 'client' not in locals() or client is None: st.error("Box client error."); st.stop()
    if not all([st.session_state.get(k) for k in ['template_file_id', 'query_file_id', 'schema_file_id']]):
        st.warning("Please select DOCX template, TXT/CSV query, and JSON schema files."); st.stop()

    st.info("Processing selected files and generating mapping...")
    progress_bar_main = st.progress(0.0, text="Initializing...")
    try:
        progress_bar_main.progress(0.1, text="Downloading files...")
        template_content = download_box_file(client, st.session_state['template_file_id'])
        query_content = download_box_file(client, st.session_state['query_file_id'])
        # schema_content is not directly used for prompt text if AI uses the file by ID
        # schema_content = download_box_file(client, st.session_state['schema_file_id']) 
        
        progress_bar_main.progress(0.2, text="Extracting merge fields...")
        merge_fields = extract_merge_fields(template_content)
        if merge_fields: st.success(f"Extracted {len(merge_fields)} merge fields.")
        else: st.warning("No merge fields found.")
        
        progress_bar_main.progress(0.3, text="Parsing Conga query...")
        conga_fields = parse_conga_query(query_content, st.session_state['query_file_type'])
        # parse_conga_query now handles its own st.success/info/warning
        
        progress_bar_main.progress(0.4, text="Validating schema file ID...")
        schema_file_id_for_ai = st.session_state.get('schema_file_id')
        if not schema_file_id_for_ai: st.error("Schema file ID missing for AI call."); st.stop()
        st.success(f"Schema file ID {schema_file_id_for_ai} will be used for AI context.")

        if not merge_fields: st.error("No merge fields to map."); st.stop()

        full_mapping_df = pd.DataFrame()
        
        progress_bar_main.progress(0.5, text="Calling Box AI (single call with full schema context)...")
        prompt_text = generate_prompt_single_call(merge_fields or [], conga_fields or [])
        ai_response_text = call_box_ai(prompt_text, schema_file_id_for_ai, st.secrets["box"]["BOX_DEVELOPER_TOKEN"])
        
        progress_bar_main.progress(0.8, text="Processing AI response...")
        if ai_response_text:
            mapping_df_chunk = convert_response_to_df(ai_response_text)
            if not mapping_df_chunk.empty:
                full_mapping_df = mapping_df_chunk # Since it's a single call, no concat needed
        else:
            st.warning("No response text received from Box AI to process.")
        
        progress_bar_main.progress(1.0, text="Completed!")
        progress_bar_main.empty()

        if not full_mapping_df.empty:
            # Duplicates might still occur if AI lists the same mapping multiple times in its single response
            full_mapping_df.drop_duplicates(subset=['CongaField', 'BoxField'], inplace=True, keep='first')
            st.subheader("Generated Field Mapping")
            st.dataframe(full_mapping_df)
            csv_buffer = io.StringIO()
            full_mapping_df.to_csv(csv_buffer, index=False)
            st.download_button("Download Mapping CSV", csv_buffer.getvalue(), "field_mapping.csv", "text/csv")
        else:
            st.warning("No valid field mappings generated by Box AI.")
    
    except Exception as e:
        st.error(f"Processing error: {str(e)}")
        import traceback
        st.text(traceback.format_exc())
        if 'progress_bar_main' in locals() and progress_bar_main is not None: progress_bar_main.empty()
