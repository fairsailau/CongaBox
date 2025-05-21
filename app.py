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
            # Provide a descriptive label, hidden for cleaner UI if needed (requires newer Streamlit for label_visibility)
            item_cols[2].button("Open", key=f"open_folder_{item.id}", 
                                on_click=lambda folder_id_val=item.id: st.session_state.update({"current_folder": folder_id_val}))
        else:
            item_cols[0].write(f"📄 {item.name}")
            item_cols[1].write(item.type.capitalize())
            
            file_extension = item.name.split('.')[-1].lower() if '.' in item.name else ''
            
            # For checkboxes, an empty label "" is used. 
            # To fix warnings: item_cols[2].checkbox(label=" ", key=..., label_visibility="collapsed")
            # but label_visibility requires Streamlit 1.17+. Current logs show older version.
            # For now, empty labels are functional.
            if file_extension == 'docx':
                selected = item_cols[2].checkbox("", key=f"select_template_{item.id}", 
                                                  value=st.session_state.get('template_file_id') == item.id)
                if selected:
                    st.session_state['template_file_id'] = item.id
                    st.session_state['template_file_name'] = item.name
                    st.session_state['template_file_type'] = 'docx'
                elif st.session_state.get('template_file_id') == item.id:
                     st.session_state.pop('template_file_id', None)
                     st.session_state.pop('template_file_name', None)
                     st.session_state.pop('template_file_type', None)

            elif file_extension in ['txt', 'csv']:
                selected = item_cols[2].checkbox("", key=f"select_query_{item.id}", 
                                                  value=st.session_state.get('query_file_id') == item.id)
                if selected:
                    st.session_state['query_file_id'] = item.id
                    st.session_state['query_file_name'] = item.name
                    st.session_state['query_file_type'] = file_extension
                elif st.session_state.get('query_file_id') == item.id:
                     st.session_state.pop('query_file_id', None)
                     st.session_state.pop('query_file_name', None)
                     st.session_state.pop('query_file_type', None)
            
            elif file_extension == 'json':
                selected = item_cols[2].checkbox("", key=f"select_schema_{item.id}", 
                                                  value=st.session_state.get('schema_file_id') == item.id)
                if selected:
                    st.session_state['schema_file_id'] = item.id
                    st.session_state['schema_file_name'] = item.name
                    st.session_state['schema_file_type'] = 'json'
                elif st.session_state.get('schema_file_id') == item.id:
                     st.session_state.pop('schema_file_id', None)
                     st.session_state.pop('schema_file_name', None)
                     st.session_state.pop('schema_file_type', None)

def download_box_file(client, file_id):
    file_content = client.file(file_id).content()
    return file_content

def extract_merge_fields(docx_content):
    with io.BytesIO(docx_content) as docx_bytes:
        document = Document(docx_bytes)
        fields = set()
        pattern = r"\{\{(.*?)(\||\}\})"
        for para in document.paragraphs:
            matches = re.findall(pattern, para.text)
            for match in matches:
                fields.add(match[0].strip())
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para_in_cell in cell.paragraphs:
                        matches = re.findall(pattern, para_in_cell.text)
                        for match in matches:
                            fields.add(match[0].strip())
        return list(fields)

def parse_conga_query(file_content, file_type):
    if file_type == 'csv':
        try:
            df = pd.read_csv(io.BytesIO(file_content))
            query_headers = list(df.columns)

            if len(query_headers) == 1 and \
               "select " in query_headers[0].lower() and \
               " from " in query_headers[0].lower():
                
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
                    else:
                        st.warning("SOQL in CSV header matched regex, but no fields were extracted. Using full query string as field name.")
                        return query_headers 
                else:
                    st.warning("CSV header looks like SOQL but could not be parsed for individual fields. Using full query string as field name.")
                    return query_headers 
            else: 
                return query_headers 
        except Exception as e:
            st.warning(f"Could not parse CSV query file: {e}. Defaulting to empty field list.")
            return []
    else:  # txt 
        text = file_content.decode("utf-8")
        match = re.search(r"select\s+(.+?)\s+from", text, re.IGNORECASE | re.DOTALL)
        if match:
            fields_segment = match.group(1)
            raw_fields = [f.strip() for f in fields_segment.split(",")] 
            parsed_fields = [f for f in raw_fields if f] 
            return parsed_fields
        st.warning(f"Conga query regex (for TXT files) did not find fields in the provided text.")
        return []

def flatten_json(y, prefix=''):
    out = {}
    def flatten(x, name=''):
        if isinstance(x, dict):
            for a in x:
                flatten(x[a], f'{name}{a}.')
        elif isinstance(x, list):
            for i, a in enumerate(x):
                flatten(a, f'{name}{i}.')
        else:
            out[name[:-1]] = str(x)
    flatten(y, prefix)
    return out

def chunk_dict(d, chunk_size=50):
    items = list(d.items())
    for i in range(0, len(items), chunk_size):
        yield dict(items[i:i + chunk_size])

def generate_prompt(merge_fields, conga_fields, schema_chunk):
    merge_fields_str = ", ".join(merge_fields)
    conga_fields_str = ", ".join(conga_fields)
    
    schema_text = "\n".join([f"- {k}: (example value: {v})" for k, v in schema_chunk.items()])
    
    prompt = (
        "You are an AI assistant helping map fields from a Conga document generation system to a Box Doc Gen system, "
        "which uses a Salesforce schema.\n\n"
        "Conga Template Merge Fields:\n"
        f"{merge_fields_str}\n\n"
        "Fields from Conga Query (likely Salesforce source fields):\n"
        f"{conga_fields_str}\n\n"
        "Partial Salesforce Schema (Path: Example Value from a record):\n"
        f"{schema_text}\n\n"
        "Task: For each 'Conga Template Merge Field', find the best matching field from the 'Salesforce Schema'. "
        "The 'Fields from Conga Query' can provide hints about the original Salesforce source of the Conga fields.\n"
        "Respond ONLY in CSV format with three columns: CongaField,BoxField,FieldType.\n"
        "CongaField: The exact merge field name from the Conga template.\n"
        "BoxField: The corresponding full path from the Salesforce Schema (e.g., Account.Name, Opportunity.LineItems.0.Product.Name).\n"
        "FieldType: The data type of the BoxField if known from schema or inferable (e.g., Text, Number, Date, Boolean, RichText).\n"
        "If a direct match is not clear, use your best judgment or leave BoxField empty for that CongaField.\n"
        "Example Row: CongaAmountField,Opportunity.Amount,Currency"
    )
    return prompt

def call_box_ai(prompt, grounding_file_id, developer_token):
    url = "https://api.box.com/2.0/ai/text_gen"
    headers = {
        "Authorization": f"Bearer {developer_token}",
        "Content-Type": "application/json"
    }
    
    items_payload = []
    if grounding_file_id:
        items_payload.append({
            "type": "file",
            "id": str(grounding_file_id), 
            "content_type": "text_content"
        })
    else:
        st.error("CRITICAL: grounding_file_id was not provided to call_box_ai, but API requires it.")
        # API will likely fail again if items_payload is empty and it's required.

    data = {
        "prompt": prompt,
        "items": items_payload
    }
    
    st.info("Box AI Request Payload (see console/logs for full details if truncated in UI):")
    st.json(data) 
    print("--- Sending Box AI Request ---")
    try:
        print(f"URL: {url}")
        print(f"Headers: {json.dumps(headers, indent=2)}")
        print(f"Payload: {json.dumps(data, indent=2)}")
    except TypeError:
        print(f"Payload (raw, error during json.dumps for logging): {data}")
    print("-----------------------------")

    response = requests.post(url, headers=headers, json=data)
    
    print(f"--- Received Box AI Response ---")
    print(f"Status Code: {response.status_code}")
    try:
        response_json = response.json()
        print(f"Response Body: {json.dumps(response_json, indent=2)}")
    except json.JSONDecodeError:
        print(f"Response Body (Non-JSON): {response.text}")
    except Exception as e:
        print(f"Could not process Box AI response for logging: {e}")
        print(f"Raw Response Text: {response.text}")
    print("-----------------------------")

    if response.status_code == 200:
        return response.json().get("completion", "")
    else:
        st.error(f"Box AI request failed: {response.status_code} — {response.text}")
        return None

def convert_response_to_df(text):
    if not text or not isinstance(text, str):
        st.warning("AI response was empty or not a string.")
        return pd.DataFrame()

    lines = text.strip().splitlines()
    
    header_idx = -1
    for i, line in enumerate(lines):
        if "CongaField" in line and "BoxField" in line:
            header_idx = i
            break
    
    if header_idx == -1:
        st.warning("CSV header (CongaField,BoxField,FieldType) not found in AI response.")
        st.text_area("AI Response (for debugging)", text, height=150)
        return pd.DataFrame()

    header = [h.strip() for h in lines[header_idx].split(",")]
    data_lines = lines[header_idx+1:]
    
    data = []
    for line in data_lines:
        split_line = [val.strip() for val in line.split(",")]
        if len(split_line) == len(header):
            data.append(split_line)
        elif len(split_line) > len(header) and len(header) == 3:
            data.append([split_line[0], split_line[1], ",".join(split_line[2:])])
        else:
            st.warning(f"Skipping malformed CSV line (expected {len(header)} columns, got {len(split_line)}): {line}")

    if not data:
        st.warning("No valid data rows found in AI response after header.")
        st.text_area("AI Response (for debugging)", text, height=150)
        return pd.DataFrame()
        
    return pd.DataFrame(data, columns=header)

# Initialize session state
if 'current_folder' not in st.session_state:
    st.session_state['current_folder'] = "0"

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
            
    except Exception as e:
        st.error(f"Error connecting to Box or browsing folders: {str(e)}")
        st.caption("Please check your Box credentials in secrets and network. Client may not be initialized.")
        client = None 

if st.button("Generate Field Mapping"):
    if 'client' not in locals() or client is None:
        st.error("Box client not initialized. Please check Box connection in the sidebar.")
        st.stop()

    if not all([st.session_state.get(key) for key in ['template_file_id', 'query_file_id', 'schema_file_id']]):
        st.warning("Please select a DOCX template, a TXT/CSV query file, and a JSON schema file from Box.")
        st.stop()

    st.info("Processing selected files and generating mapping...")
    
    try:
        template_content = download_box_file(client, st.session_state['template_file_id'])
        query_content = download_box_file(client, st.session_state['query_file_id'])
        schema_content = download_box_file(client, st.session_state['schema_file_id'])
        
        merge_fields = extract_merge_fields(template_content)
        if merge_fields:
            st.success(f"Extracted {len(merge_fields)} merge fields from template: {', '.join(merge_fields[:5])}{'...' if len(merge_fields) > 5 else ''}")
        else:
            st.warning("No merge fields found in the template.")
        
        conga_fields = parse_conga_query(query_content, st.session_state['query_file_type'])
        if conga_fields:
             # Message now comes from within parse_conga_query or is a generic success
             pass # St.success/warning is handled inside parse_conga_query for detailed CSV parsing
        else:
            st.warning("No fields extracted from the Conga query file.")

        try:
            schema_data = json.loads(schema_content.decode('utf-8'))
        except json.JSONDecodeError as json_err:
            st.error(f"Invalid JSON in schema file: {json_err}")
            st.stop()

        flat_schema = flatten_json(schema_data)
        if flat_schema:
            st.success(f"Flattened schema into {len(flat_schema)} fields.")
        else:
            st.warning("Schema file was empty or could not be flattened.")
        
        if not merge_fields:
            st.error("Cannot generate mapping without merge fields from the template.")
            st.stop()
        if not flat_schema:
             st.error("Cannot generate mapping without a valid, flattened schema.")
             st.stop()

        schema_file_id_for_ai = st.session_state.get('schema_file_id')
        if not schema_file_id_for_ai:
            st.error("Schema file ID is missing. Cannot make Box AI call as it requires a grounding item.")
            st.stop()

        full_mapping_df = pd.DataFrame()
        schema_chunk_size = 30 
        num_chunks = (len(flat_schema) + schema_chunk_size - 1) // schema_chunk_size if flat_schema else 1
        progress_bar = st.progress(0.0)
        processed_chunks = 0

        for i, schema_chunk in enumerate(chunk_dict(flat_schema, chunk_size=schema_chunk_size)):
            processed_chunks +=1
            st.info(f"Calling Box AI (part {processed_chunks} of {num_chunks} schema chunks)...")
            progress_bar.progress(processed_chunks / num_chunks, text=f"Processing schema chunk {processed_chunks}/{num_chunks}")

            prompt_text = generate_prompt(merge_fields or [], conga_fields or [], schema_chunk)
            
            ai_response_text = call_box_ai(prompt_text, schema_file_id_for_ai, st.secrets["box"]["BOX_DEVELOPER_TOKEN"])
            
            if ai_response_text:
                mapping_df_chunk = convert_response_to_df(ai_response_text)
                if not mapping_df_chunk.empty:
                    full_mapping_df = pd.concat([full_mapping_df, mapping_df_chunk], ignore_index=True)
            else:
                st.warning(f"No response or error from Box AI for schema chunk {processed_chunks}.")
        
        progress_bar.empty()

        if not full_mapping_df.empty:
            full_mapping_df.drop_duplicates(subset=['CongaField', 'BoxField'], inplace=True)
            
            st.subheader("Generated Field Mapping")
            st.dataframe(full_mapping_df)
            
            csv_buffer = io.StringIO()
            full_mapping_df.to_csv(csv_buffer, index=False)
            st.download_button(
                label="Download Mapping CSV",
                data=csv_buffer.getvalue(),
                file_name="field_mapping.csv",
                mime="text/csv"
            )
        else:
            st.warning("No valid field mappings were generated by Box AI across all schema chunks.")
    
    except Exception as e:
        st.error(f"An error occurred during processing: {str(e)}")
        import traceback
        st.text(traceback.format_exc())
        if 'progress_bar' in locals() and progress_bar is not None:
            progress_bar.empty()
