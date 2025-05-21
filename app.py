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
        raw_fields = set()
        pattern = r"\{\{(.*?)\}\}" 
        
        for para in document.paragraphs:
            matches = re.findall(pattern, para.text)
            for match_text in matches: raw_fields.add(match_text.strip())
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para_in_cell in cell.paragraphs:
                        matches = re.findall(pattern, para_in_cell.text)
                        for match_text in matches: raw_fields.add(match_text.strip())
        
        cleaned_fields = set()
        for field in raw_fields:
            core_field = re.split(r'\s*\\@|\s*\|', field, 1)[0] # Split by \@ or |
            cleaned_fields.add(core_field.strip())
        
        if len(raw_fields) != len(cleaned_fields) or any(r != c for r, c in zip(sorted(list(raw_fields)), sorted(list(cleaned_fields)))):
            st.info(f"Cleaned merge fields. Original unique tags found: {len(raw_fields)}. Core fields for AI: {len(cleaned_fields)}")
            # Example: st.caption(f"Example raw: '{list(raw_fields)[0]}', Cleaned: '{re.split(r'\\s*\\\\@|\\s*\\|', list(raw_fields)[0], 1)[0].strip()}'")
        return list(cleaned_fields)

def parse_conga_query(file_content, file_type):
    all_parsed_fields = set()
    s_object_names = set()

    def extract_from_soql_list(query_text_list):
        processed_queries_count = 0
        for query_text_item in query_text_list:
            query_text_item = str(query_text_item).strip()
            if not query_text_item: continue
            
            from_match = re.search(r"from\s+([a-zA-Z0-9_]+(?:__c|__r)?)\b", query_text_item, re.IGNORECASE)
            if from_match:
                s_object_names.add(from_match.group(1))

            if "select " in query_text_item.lower() and " from " in query_text_item.lower():
                processed_queries_count +=1
                select_match = re.search(r"select\s+(.+?)\s+from", query_text_item, re.IGNORECASE | re.DOTALL)
                if select_match:
                    fields_segment = select_match.group(1)
                    current_query_fields = re.findall(r"([\w\._()]+(?:\s+AS\s+[\w\._]+)?)(?:\s*,|\s+FROM)", fields_segment + " FROM", re.IGNORECASE | re.DOTALL)
                    all_parsed_fields.update([f.strip() for f in current_query_fields if f.strip()])
        return processed_queries_count

    if file_type == 'csv':
        try:
            df = pd.read_csv(io.BytesIO(file_content), header=None, skip_blank_lines=True)
            queries_to_process = []
            if not df.empty:
                first_cell_value = str(df.iloc[0, 0]).lower().strip()
                if "select " in first_cell_value and " from " in first_cell_value:
                    queries_to_process = df.iloc[:, 0].dropna().astype(str).tolist()
                elif len(df) > 1: 
                    second_cell_value = str(df.iloc[1,0]).lower().strip()
                    if "select " in second_cell_value and " from " in second_cell_value:
                         queries_to_process = df.iloc[1:, 0].dropna().astype(str).tolist()
                    else: 
                        df_with_header = pd.read_csv(io.BytesIO(file_content))
                        all_parsed_fields.update([str(col).strip() for col in df_with_header.columns])
                        st.info(f"CSV: No SOQL found in first data column. Using headers as fields: {list(all_parsed_fields)}")
                        return list(all_parsed_fields), list(s_object_names)
                else: 
                    if "select " in first_cell_value and " from " in first_cell_value:
                        queries_to_process = df.iloc[:,0].dropna().astype(str).tolist()
                    else: 
                        df_with_header = pd.read_csv(io.BytesIO(file_content))
                        all_parsed_fields.update([str(col).strip() for col in df_with_header.columns])
                        st.info(f"CSV: Single row, no SOQL. Using headers as fields: {list(all_parsed_fields)}")
                        return list(all_parsed_fields), list(s_object_names)

                if queries_to_process:
                    st.info(f"CSV: Found {len(queries_to_process)} potential queries in the first column.")
                    num_processed = extract_from_soql_list(queries_to_process)
                    if num_processed > 0 and (all_parsed_fields or s_object_names):
                        st.success(f"CSV: Extracted {len(all_parsed_fields)} unique fields and {len(s_object_names)} SObjects from {num_processed} SOQL queries.")
                    elif num_processed > 0 :
                        st.warning(f"CSV: Processed {num_processed} SOQL queries but extracted no fields or SObjects.")
                    else:
                        st.warning("CSV: No processable SOQL queries found in the first column.")
                else: # This case means the CSV was empty or didn't fit any query patterns
                    st.warning("CSV: No queries to process from the first column after initial checks.")
                    # Fallback to trying to use headers if any were read by default
                    df_original_headers = pd.read_csv(io.BytesIO(file_content)) 
                    if not df_original_headers.empty:
                        all_parsed_fields.update([str(col).strip() for col in df_original_headers.columns])
                        if all_parsed_fields:
                             st.info(f"CSV: Fallback - Using column headers as fields: {list(all_parsed_fields)}")


        except Exception as e:
            st.error(f"Error parsing CSV query file: {e}. Trying to read as plain text SOQL.")
            try:
                text_content = file_content.decode("utf-8")
                return parse_conga_query(text_content.encode("utf-8"), 'txt')
            except Exception as e_txt:
                st.error(f"Fallback to TXT parsing also failed: {e_txt}")
                return [], []
    
    elif file_type == 'txt':  
        text = file_content.decode("utf-8")
        queries = re.split(r';\s*\n?|\n\s*(?=\n|\Z)|(?<=\n)\s*\n+(?=\S)', text) 
        num_processed = extract_from_soql_list(queries)
        if num_processed > 0 and (all_parsed_fields or s_object_names):
             st.success(f"TXT: Extracted {len(all_parsed_fields)} unique fields and {len(s_object_names)} SObjects from {num_processed} SOQL queries.")
        elif num_processed > 0:
             st.warning(f"TXT: Processed {num_processed} SOQL queries but extracted no fields or SObjects.")
        else:
             st.warning("TXT: No SOQL queries found to process.")
    
    else: 
         st.error(f"Unsupported query file type: {file_type}")

    return list(all_parsed_fields), list(s_object_names)

# Refined prompt strategy: Include SObjects and a *limited sample* of conga_query_fields
def generate_prompt_single_call(merge_fields, conga_query_fields, conga_query_sobjects):
    merge_fields_str = ", ".join(sorted(list(set(merge_fields))))
    conga_sobjects_str = ", ".join(sorted(list(set(conga_query_sobjects)))) if conga_query_sobjects else "Not specified, infer from schema"
    
    max_query_fields_in_prompt = 50 # Adjust this limit as needed
    unique_query_fields = sorted(list(set(conga_query_fields)))
    
    if len(unique_query_fields) > max_query_fields_in_prompt:
        conga_fields_display_str = f"{', '.join(unique_query_fields[:max_query_fields_in_prompt])} (and {len(unique_query_fields) - max_query_fields_in_prompt} more, not listed here but present in the queries)"
    else:
        conga_fields_display_str = ", ".join(unique_query_fields) if unique_query_fields else "No specific fields extracted from queries, rely on SObject list and schema file."

    query_context_instruction = (
        "The Conga queries, which were the original source for the Conga template fields, primarily involve the "
        f"following Salesforce SObjects: {conga_sobjects_str}. "
        "Additionally, some specific fields referenced in these queries include: "
        f"{conga_fields_display_str}. "
        "Use the provided schema file to find fields within these SObjects or related SObjects. "
        "Prioritize mappings based on the SObject context and then the specific query fields if helpful.\n\n"
    )

    prompt = (
        "You are an AI assistant helping map fields from a Conga document generation system to a Box Doc Gen system. "
        "You have been provided with a Salesforce schema file as context (see the 'items' passed to the API call). Please use this schema file extensively for your mappings.\n\n"
        "Conga Template Merge Fields (these are the fields you need to map from the Conga template to the Salesforce schema):\n"
        f"{merge_fields_str}\n\n"
        f"{query_context_instruction}"
        "Task: For each 'Conga Template Merge Field' listed above, find the most relevant field path from the provided Salesforce schema file. "
        "Ensure the BoxField path is a valid path from the schema file.\n"
        "Respond ONLY in CSV format with three columns: CongaField,BoxField,FieldType.\n"
        "1. CongaField: The exact merge field name from the 'Conga Template Merge Fields' list.\n"
        "2. BoxField: The corresponding full path from the Salesforce Schema file (e.g., Account.Name, $User.FirstName, Opportunity.LineItems.0.Product.Name). Refer to the provided schema file for these paths. If a CongaField seems to reference a related object not directly listed under the SObjects above, use the schema file to find the correct path (e.g. if CongaField is 'CONTACT_ACCOUNT_NAME' and main SObject is Contact, BoxField could be 'Contact.Account.Name').\n"
        "3. FieldType: The data type of the BoxField if known from the schema file or inferable (e.g., Text, Number, Date, Boolean, RichText, Id, Picklist).\n"
        "If a direct match for a CongaField is not clear in the schema, even after considering relationships, leave the BoxField and FieldType empty for that CongaField. Do not invent fields.\n"
        "Prioritize exact or very close name matches within the relevant SObject contexts.\n"
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
    print(f"--- Sending Box AI Request ---\nURL: {url}\nHeaders: {json.dumps(headers, indent=2)}\nPayload Length: {len(json.dumps(data))}\n-----------------------------")
    
    response = requests.post(url, headers=headers, json=data)
    
    print(f"--- Received Box AI Response ---\nStatus Code: {response.status_code}")
    response_text_for_return = None
    try: 
        response_data = response.json()
        print(f"Response Body (first 500 chars): {json.dumps(response_data, indent=2)[:500]}...")
        if response.status_code == 200:
            response_text_for_return = response_data.get("answer", "") 
    except json.JSONDecodeError: 
        print(f"Response Body (Non-JSON): {response.text[:500]}...")
        if response.status_code == 200: 
            response_text_for_return = response.text 
    except Exception as e: 
        print(f"Could not process Box AI response for logging: {e}\nRaw (first 500 chars): {response.text[:500]}...")
    print("-----------------------------")

    if response.status_code == 200: 
        return response_text_for_return
    else: 
        st.error(f"Box AI request failed: {response.status_code} — {response.text}")
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
    expected_headers_check = ["CongaField", "BoxField"] 
    for i, line in enumerate(lines):
        if all(expected_header in line for expected_header in expected_headers_check):
            header_idx = i
            break
    
    final_columns = ["CongaField", "BoxField", "FieldType"]
    if header_idx == -1:
        st.warning(f"CSV header (containing CongaField, BoxField) not found in processed AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame(columns=final_columns)

    header_from_ai_raw = [h.strip() for h in lines[header_idx].split(",")]
    # Map AI headers to our expected final_columns, being flexible with AI's column naming
    header_map = {}
    temp_final_cols_lower = [fc.lower() for fc in final_columns]
    
    for ai_h in header_from_ai_raw:
        ai_h_lower = ai_h.lower()
        if ai_h_lower in temp_final_cols_lower:
            original_col_name = final_columns[temp_final_cols_lower.index(ai_h_lower)]
            header_map[ai_h] = original_col_name
    
    # If AI didn't provide expected headers, this will be empty or partial.
    # We will construct DataFrame with final_columns and fill data based on mapped AI headers.

    data_lines = lines[header_idx+1:]
    processed_data_list = []

    for line_num, line_content in enumerate(data_lines):
        line_content_stripped = line_content.strip()
        if not line_content_stripped: continue
        
        if len(line_content_stripped) > 0 and \
           not line_content_stripped[0].isalnum() and \
           line_content_stripped[0] not in ['{', '"', '('] and \
           line_content_stripped.count(',') < 1 : 
                 st.info(f"Skipping likely non-data/comment line: '{line_content_stripped}'")
                 continue

        split_line_values = [val.strip() for val in line_content_stripped.split(",")]
        
        # Create a dictionary for the current row based on AI's headers
        row_dict_from_ai = {}
        for i, val in enumerate(split_line_values):
            if i < len(header_from_ai_raw): # Ensure we don't go out of bounds for AI's header
                ai_header_name = header_from_ai_raw[i]
                if ai_header_name in header_map: # If this AI header maps to one of our final_columns
                    row_dict_from_ai[header_map[ai_header_name]] = val
                # else: # AI provided a column we are not expecting, ignore for now
                #    pass
            # Handle cases where FieldType might have commas
            elif header_map.get(header_from_ai_raw[-1]) == "FieldType" and len(header_from_ai_raw) > 0: 
                # If the last mapped AI header is FieldType, append to it
                 row_dict_from_ai["FieldType"] += "," + val


        # Ensure all final_columns are present in the dict for DataFrame consistency
        final_row_dict = {col: row_dict_from_ai.get(col, "") for col in final_columns}
        processed_data_list.append(final_row_dict)
            
    if not processed_data_list:
        st.warning("No valid data rows extracted from AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame(columns=final_columns)
        
    return pd.DataFrame(processed_data_list, columns=final_columns)


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
        
        progress_bar_main.progress(0.2, text="Extracting merge fields...")
        merge_fields = extract_merge_fields(template_content)
        if merge_fields: st.success(f"Extracted {len(merge_fields)} merge fields.")
        else: st.warning("No merge fields found.")
        
        progress_bar_main.progress(0.3, text="Parsing Conga query...")
        conga_fields, conga_sobjects = parse_conga_query(query_content, st.session_state['query_file_type'])
        
        progress_bar_main.progress(0.4, text="Validating schema file ID...")
        schema_file_id_for_ai = st.session_state.get('schema_file_id')
        if not schema_file_id_for_ai: st.error("Schema file ID missing for AI call."); st.stop()
        st.success(f"Schema file ID {schema_file_id_for_ai} will be used for AI context.")

        if not merge_fields: st.error("No merge fields to map."); st.stop()

        full_mapping_df = pd.DataFrame()
        
        progress_bar_main.progress(0.5, text="Calling Box AI (single call with full schema context)...")
        prompt_text = generate_prompt_single_call(merge_fields or [], conga_fields or [], conga_sobjects or [])
        ai_response_text = call_box_ai(prompt_text, schema_file_id_for_ai, st.secrets["box"]["BOX_DEVELOPER_TOKEN"])
        
        progress_bar_main.progress(0.8, text="Processing AI response...")
        if ai_response_text:
            mapping_df_from_ai = convert_response_to_df(ai_response_text) 
            if not mapping_df_from_ai.empty:
                full_mapping_df = mapping_df_from_ai
        else:
            st.warning("No response text received from Box AI to process.")
        
        progress_bar_main.progress(1.0, text="Completed!")
        progress_bar_main.empty()

        if not full_mapping_df.empty:
            expected_cols = ["CongaField", "BoxField", "FieldType"]
            # Ensure all expected columns are present, add if missing
            for col in expected_cols:
                if col not in full_mapping_df.columns:
                    full_mapping_df[col] = "" 
            full_mapping_df = full_mapping_df[expected_cols] # Reorder/select

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
