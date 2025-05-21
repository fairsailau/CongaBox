import streamlit as st
from docx import Document
import pandas as pd
import json
import re
import io
import requests
from boxsdk import OAuth2, Client

st.set_page_config(page_title="Conga to Box Doc Gen", layout="centered")

# --- AI Model Configuration ---
# IMPORTANT: Replace placeholder model IDs with actual valid model IDs provided by Box
# for the /ai/text_gen endpoint if you want to use specific models.
# If a model ID is set to None or an empty string, the 'model' parameter will be omitted,
# and Box will use its default text generation model.
BOX_AI_MODELS = {
    "Default (Let Box Choose)": None,
    # Example: "box-claude-2.1-gen-001" (This is a hypothetical ID, get actual IDs from Box)
    "Example Model A (e.g., Balanced)": "placeholder-model-id-a", 
    "Example Model B (e.g., Creative/Powerful)": "placeholder-model-id-b",
}
# --- End AI Model Configuration ---

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
            # Split by \@ or | or even & to remove common formatters/parameters
            core_field = re.split(r'\s*(?:\\@|\||&)', field, 1)[0] 
            cleaned_fields.add(core_field.strip())
        
        if len(raw_fields) != len(cleaned_fields) or any(r != c for r, c in zip(sorted(list(raw_fields)), sorted(list(cleaned_fields)))):
            st.info(f"Cleaned merge fields. Original unique tags found: {len(raw_fields)}. Core fields for AI: {len(cleaned_fields)}")
        return list(cleaned_fields)


def parse_conga_query(file_content, file_type):
    sobject_to_fields_map = {}
    all_unique_fields = set()
    all_unique_sobjects = set()

    def process_soql_string(query_text):
        query_text = str(query_text).strip()
        if not ("select " in query_text.lower() and " from " in query_text.lower()):
            return False

        from_match = re.search(r"from\s+([a-zA-Z0-9_]+(?:__c|__r)?)\b", query_text, re.IGNORECASE)
        current_sobject = from_match.group(1) if from_match else None
        
        if current_sobject:
            all_unique_sobjects.add(current_sobject)
            if current_sobject not in sobject_to_fields_map:
                sobject_to_fields_map[current_sobject] = set()

        select_match = re.search(r"select\s+(.+?)\s+from", query_text, re.IGNORECASE | re.DOTALL)
        if select_match:
            fields_segment = select_match.group(1)
            current_query_fields = re.findall(r"([\w\._()]+(?:\s+AS\s+[\w\._]+)?)(?:\s*,|\s+FROM)", fields_segment + " FROM", re.IGNORECASE | re.DOTALL)
            
            cleaned_query_fields = {f.strip() for f in current_query_fields if f.strip()}
            all_unique_fields.update(cleaned_query_fields)
            if current_sobject:
                sobject_to_fields_map[current_sobject].update(cleaned_query_fields)
            return True
        return False

    queries_to_parse = []
    if file_type == 'csv':
        try:
            df = pd.read_csv(io.BytesIO(file_content), header=None, usecols=[0], squeeze=True, skip_blank_lines=True)
            if isinstance(df, pd.Series):
                queries_to_parse = df.dropna().astype(str).tolist()
            elif isinstance(df, pd.DataFrame) and not df.empty:
                 queries_to_parse = df.iloc[:,0].dropna().astype(str).tolist()
            
            if queries_to_parse:
                 st.info(f"CSV: Processing {len(queries_to_parse)} lines from the first column as potential SOQL queries.")
            else: # Fallback if squeeze didn't work or CSV not as expected
                st.warning("CSV query file could not be parsed as a list of queries. Trying column headers as fallback.")
                df_headers = pd.read_csv(io.BytesIO(file_content), nrows=0)
                all_unique_fields.update([str(col).strip() for col in df_headers.columns])
                st.info(f"CSV (Fallback): Using column headers as query fields: {list(all_unique_fields)}")
                # For this fallback, we don't know the SObject, so map is less useful.
                # We could put all fields under a generic key or leave sobject_to_fields_map empty.
                if all_unique_fields:
                    sobject_to_fields_map["UnknownFromCSVHeaders"] = set(all_unique_fields)
                return list(all_unique_fields), list(all_unique_sobjects) # SObjects might be empty here

        except Exception as e:
            st.error(f"Error parsing CSV query file: {e}. Attempting fallback to TXT parsing.")
            try: 
                return parse_conga_query(file_content, 'txt')
            except Exception as e_txt:
                 st.error(f"Fallback to TXT parsing also failed: {e_txt}")
                 return {}, set(), set()
    
    elif file_type == 'txt':
        text_content = file_content.decode("utf-8")
        queries_to_parse = [q.strip() for q in re.split(r';\s*\n?|\n\s{2,}\n?|\n\s*$', text_content, flags=re.MULTILINE) if q.strip()]
        if queries_to_parse: st.info(f"TXT: Found {len(queries_to_parse)} potential SOQL queries.")
    else:
        st.error(f"Unsupported query file type: {file_type}")
        return {}, set(), set()

    processed_count = 0
    for query_str in queries_to_parse:
        if process_soql_string(query_str):
            processed_count += 1
    
    if processed_count > 0:
        st.success(f"{file_type.upper()}: Processed {processed_count} SOQL queries. Found {len(all_unique_fields)} unique fields across {len(all_unique_sobjects)} SObjects.")
    elif queries_to_parse: # If there were things to parse but none were processed
        st.warning(f"{file_type.upper()}: No SOQL queries were successfully processed from {len(queries_to_parse)} candidates.")
    else: # No queries found at all
        st.warning(f"{file_type.upper()}: No query strings found to process.")


    final_sobject_to_fields_map = {k: sorted(list(v)) for k, v in sobject_to_fields_map.items()}
    return final_sobject_to_fields_map, all_unique_fields, all_unique_sobjects


def generate_prompt_single_call(merge_fields, sobject_to_fields_map, all_query_sobjects):
    merge_fields_str_list = []
    for mf in sorted(list(set(merge_fields))):
        mf_lower = mf.lower()
        possible_sources = []
        for sobj, fields in sobject_to_fields_map.items():
            sobj_clean_name = sobj.lower().replace("__c", "").replace("__r", "")
            if mf_lower.startswith(sobj_clean_name) or mf_lower.replace("_", "").startswith(sobj_clean_name):
                if sobj not in [s.split(" ")[0] for s in possible_sources]:
                    possible_sources.append(f"{sobj} (prefix match)")
            else:
                for q_field in fields:
                    if mf_lower in q_field.lower() or q_field.lower() in mf_lower:
                        if sobj not in [s.split(" ")[0] for s in possible_sources]:
                             possible_sources.append(f"{sobj} (related: {q_field})")
                        break 
        hint = ""
        if possible_sources:
            hint = f" (Hint: check in SObject(s) - {'; '.join(possible_sources)[:150]})"
        merge_fields_str_list.append(f"- {mf}{hint}")

    detailed_merge_fields_list = "\n".join(merge_fields_str_list)
    all_query_sobjects_str = ", ".join(sorted(list(all_query_sobjects))) if all_query_sobjects else "Not specified, use full schema"

    prompt = (
        "You are an AI assistant helping map fields from a Conga document generation system to a Box Doc Gen system. "
        "You have been provided with a Salesforce schema file as context (see the 'items' passed to the API call). Use this schema file as the primary source of truth for Salesforce field paths.\n\n"
        "Conga Template Merge Fields to Map (each with a hint about potential source SObjects based on associated Conga queries):\n"
        f"{detailed_merge_fields_list}\n\n"
        "The Conga queries (source of the above merge fields) primarily involve these Salesforce SObjects (consult the schema file for their detailed structure):\n"
        f"{all_query_sobjects_str}\n\n"
        "Task: For each 'Conga Template Merge Field' from the list above, find its most relevant corresponding field path in the provided Salesforce schema file. "
        "Use the hints next to each merge field and the SObject list to guide your search within the schema. The hints are suggestions; the schema file is authoritative for field paths.\n"
        "Respond ONLY in CSV format with three columns: CongaField,BoxField,FieldType.\n"
        "1. CongaField: The exact Conga merge field from the list (without the hint part).\n"
        "2. BoxField: The full path from the Salesforce Schema file (e.g., Account.Name, $User.Manager.FirstName, Opportunity.OpportunityLineItems.0.Product2.ProductCode). Use the '$' prefix for global objects like $User or $Organization if present in the schema. Ensure this path exists in the provided schema.\n"
        "3. FieldType: The data type of the BoxField from the schema (e.g., Text, Number, Date, Boolean, Picklist, Id, RichText, Lookup, MasterDetail).\n"
        "If a match is not clear in the schema for a CongaField, even after considering relationships suggested by the hints or schema structure, leave the BoxField and FieldType empty for that CongaField. Do not invent field paths.\n"
        "Prioritize exact or very close name matches within the relevant SObject contexts identified by the hints.\n"
        "Example Row: Template_Account_Name,Account.Name,Text"
    )
    return prompt

def call_box_ai(prompt, grounding_file_id, developer_token, model_id=None):
    url = "https://api.box.com/2.0/ai/text_gen"
    headers = {"Authorization": f"Bearer {developer_token}", "Content-Type": "application/json"}
    items_payload = []
    if grounding_file_id:
        items_payload.append({"type": "file", "id": str(grounding_file_id), "content_type": "text_content"})
    
    data = {"prompt": prompt, "items": items_payload}
    model_used_message = "Using default Box AI model."
    if model_id and model_id != "None": # Check against string "None" if it might come from selectbox
        data["model"] = model_id
        model_used_message = f"Using Box AI model: {model_id}"
    
    st.info(model_used_message)
    st.info("Box AI Request Payload (see console/logs for full details):")
    st.json(data) 
    print(f"--- Sending Box AI Request ---\nURL: {url}\nHeaders: {json.dumps(headers, indent=2)}\nModel: {data.get('model', 'Default')}\nPayload Length: {len(json.dumps(data))}\n-----------------------------")
    
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
        if response.status_code == 200: response_text_for_return = response.text 
    except Exception as e: 
        print(f"Could not process Box AI response for logging: {e}\nRaw (first 500 chars): {response.text[:500]}...")
    print("-----------------------------")

    if response.status_code == 200: return response_text_for_return
    else: st.error(f"Box AI request failed: {response.status_code} — {response.text}"); return None

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
        if all(expected_header.lower() in line.lower() for expected_header in expected_headers_check): # case-insensitive check
            header_idx = i
            break
    
    final_columns = ["CongaField", "BoxField", "FieldType"]
    if header_idx == -1:
        st.warning(f"CSV header (containing CongaField, BoxField) not found in processed AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame(columns=final_columns)

    header_from_ai_raw = [h.strip() for h in lines[header_idx].split(",")]
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
        
        row_data = {}
        # Try to map based on expected column names, being flexible with AI's header naming
        for i, col_name_expected in enumerate(final_columns):
            found_val = None
            for j, ai_header in enumerate(header_from_ai_raw):
                if ai_header.lower() == col_name_expected.lower():
                    if j < len(split_line_values):
                        found_val = split_line_values[j]
                    # If it's the last expected column and there are more split values, join them
                    if col_name_expected == final_columns[-1] and j < len(split_line_values) and len(split_line_values) > len(header_from_ai_raw):
                         found_val = ",".join(split_line_values[j:])
                    break 
            row_data[col_name_expected] = found_val if found_val is not None else ""


        # Fallback if above mapping is insufficient (e.g. AI gives fewer columns than expected)
        if not any(row_data.values()): # If all values are empty from mapping
            if len(split_line_values) == 1:
                row_data["CongaField"] = split_line_values[0]
            elif len(split_line_values) == 2:
                row_data["CongaField"] = split_line_values[0]
                row_data["BoxField"] = split_line_values[1]
            elif len(split_line_values) >= 3:
                row_data["CongaField"] = split_line_values[0]
                row_data["BoxField"] = split_line_values[1]
                row_data["FieldType"] = ",".join(split_line_values[2:]) # Join if FieldType had commas
        
        processed_data_list.append(row_data)
            
    if not processed_data_list:
        st.warning("No valid data rows extracted from AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame(columns=final_columns)
        
    return pd.DataFrame(processed_data_list, columns=final_columns)


if 'current_folder' not in st.session_state: st.session_state['current_folder'] = "0"
st.title("Conga Template to Box Doc Gen Mapper")

# --- UI for Model Selection ---
st.sidebar.subheader("AI Configuration")
# Ensure BOX_AI_MODELS is defined globally or passed appropriately
model_display_names = list(BOX_AI_MODELS.keys())
selected_model_display_name = st.sidebar.selectbox(
    "Choose Box AI Model:",
    options=model_display_names,
    index=0 # Default to the first model in the list
)
selected_model_id = BOX_AI_MODELS[selected_model_display_name]
# --- End UI for Model Selection ---


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
        sobject_to_fields_map, _, all_query_sobjects = parse_conga_query(query_content, st.session_state['query_file_type'])
        
        progress_bar_main.progress(0.4, text="Validating schema file ID...")
        schema_file_id_for_ai = st.session_state.get('schema_file_id')
        if not schema_file_id_for_ai: st.error("Schema file ID missing for AI call."); st.stop()
        st.success(f"Schema file ID {schema_file_id_for_ai} will be used for AI context.")

        if not merge_fields: st.error("No merge fields to map."); st.stop()

        full_mapping_df = pd.DataFrame()
        
        progress_bar_main.progress(0.5, text="Calling Box AI...")
        prompt_text = generate_prompt_single_call(merge_fields or [], sobject_to_fields_map, all_query_sobjects or [])
        ai_response_text = call_box_ai(prompt_text, schema_file_id_for_ai, st.secrets["box"]["BOX_DEVELOPER_TOKEN"], selected_model_id)
        
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
            for col in expected_cols:
                if col not in full_mapping_df.columns:
                    full_mapping_df[col] = "" 
            full_mapping_df = full_mapping_df[expected_cols] 

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
