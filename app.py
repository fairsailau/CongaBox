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
        pattern = r"\{\{(.*?)(\||\}\})"
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
    if file_type == 'csv':
        try:
            df = pd.read_csv(io.BytesIO(file_content))
            query_headers = list(df.columns)
            if len(query_headers) == 1 and "select " in query_headers[0].lower() and " from " in query_headers[0].lower():
                st.info("Attempting to parse SOQL query found in CSV header.")
                text = query_headers[0] 
                match = re.search(r"select\s+(.+?)\s+from", text, re.IGNORECASE | re.DOTALL)
                if match:
                    fields_segment = match.group(1)
                    raw_fields = [f.strip() for f in fields_segment.split(",")]
                    parsed_fields = [f for f in raw_fields if f] 
                    if parsed_fields:
                        st.success(f"Successfully parsed fields from SOQL in CSV header: {len(parsed_fields)} fields found.")
                        return parsed_fields
                    else: st.warning("SOQL in CSV header matched regex, but no fields were extracted. Using full query string."); return query_headers 
                else: st.warning("CSV header looks like SOQL but could not be parsed. Using full query string."); return query_headers 
            else: return query_headers 
        except Exception as e: st.warning(f"Could not parse CSV query file: {e}. Defaulting to empty."); return []
    else:  
        text = file_content.decode("utf-8")
        match = re.search(r"select\s+(.+?)\s+from", text, re.IGNORECASE | re.DOTALL)
        if match:
            fields_segment = match.group(1)
            raw_fields = [f.strip() for f in fields_segment.split(",")] 
            return [f for f in raw_fields if f] 
        st.warning(f"Conga query regex (for TXT files) did not find fields."); return []

def flatten_json(y, prefix=''): # Retained for potential future use or debugging
    out = {}
    def flatten(x, name=''):
        if isinstance(x, dict):
            for a in x: flatten(x[a], f'{name}{a}.')
        elif isinstance(x, list):
            for i, a in enumerate(x): flatten(a, f'{name}{i}.')
        else: out[name[:-1]] = str(x)
    flatten(y, prefix); return out

# def chunk_dict(d, chunk_size=50): # No longer needed for single-call strategy
#     items = list(d.items())
#     for i in range(0, len(items), chunk_size):
#         yield dict(items[i:i + chunk_size])

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
            response_text_for_return = response_data.get("answer", "") # CORRECTED
    except json.JSONDecodeError: 
        print(f"Response Body (Non-JSON): {response.text}")
        if response.status_code == 200: # If status is OK but not JSON, maybe it's plain text? Unlikely for this API.
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
        
        split_line = [val.strip() for val in line_content_stripped.split(",")]

        if len(split_line) == len(header_from_ai):
            data.append(split_line)
        elif len(split_line) > len(header_from_ai) and len(header_from_ai) == 3:
            data.append([split_line[0], split_line[1], ",".join(split_line[2:])])
        elif len(split_line) > 0 :
             st.warning(f"Skipping malformed CSV line #{header_idx+2+line_num} (cols {len(split_line)} vs {len(header_from_ai)}): '{line_content_stripped}'")

    if not data:
        st.warning("No valid data rows extracted from AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame()
        
    return pd.DataFrame(data, columns=header_from_ai)


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
        schema_content = download_box_file(client, st.session_state['schema_file_id']) # For loading JSON
        
        progress_bar_main.progress(0.2, text="Extracting merge fields...")
        merge_fields = extract_merge_fields(template_content)
        if merge_fields: st.success(f"Extracted {len(merge_fields)} merge fields.")
        else: st.warning("No merge fields found.")
        
        progress_bar_main.progress(0.3, text="Parsing Conga query...")
        conga_fields = parse_conga_query(query_content, st.session_state['query_file_type'])
        
        progress_bar_main.progress(0.4, text="Loading schema for context...")
        try: 
            schema_data_for_info = json.loads(schema_content.decode('utf-8')) # For potential info display
            st.success(f"Schema JSON file loaded successfully (will be passed to AI by ID).")
        except json.JSONDecodeError as je: st.error(f"Invalid JSON schema: {je}"); st.stop()

        if not merge_fields: st.error("No merge fields to map."); st.stop()

        schema_file_id_for_ai = st.session_state.get('schema_file_id')
        if not schema_file_id_for_ai: st.error("Schema file ID missing for AI call."); st.stop()

        full_mapping_df = pd.DataFrame()
        
        progress_bar_main.progress(0.5, text="Calling Box AI (single call with full schema context)...")
        prompt_text = generate_prompt_single_call(merge_fields or [], conga_fields or [])
        ai_response_text = call_box_ai(prompt_text, schema_file_id_for_ai, st.secrets["box"]["BOX_DEVELOPER_TOKEN"])
        
        progress_bar_main.progress(0.8, text="Processing AI response...")
        if ai_response_text:
            mapping_df_chunk = convert_response_to_df(ai_response_text)
            if not mapping_df_chunk.empty:
                full_mapping_df = pd.concat([full_mapping_df, mapping_df_chunk], ignore_index=True) # Though only one chunk now
        else:
            st.warning("No response text received from Box AI to process.") # Clarified message
        
        progress_bar_main.progress(1.0, text="Completed!")
        progress_bar_main.empty()

        if not full_mapping_df.empty:
            full_mapping_df.drop_duplicates(subset=['CongaField', 'BoxField'], inplace=True)
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
