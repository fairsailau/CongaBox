import streamlit as st
from docx import Document
import pandas as pd
import json
import re
import io
import requests
from boxsdk import OAuth2, Client
# from boxsdk.object.file import File # Not explicitly used in the provided snippet parts that are modified

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
    # Ensure 'name' and 'parent' fields are fetched for the folder object.
    # 'parent' is crucial for breadcrumb construction. 'name' for display.
    folder = client.folder(folder_id).get(fields=['name', 'parent'])
    items = client.folder(folder_id).get_items()
    return folder, items

def navigate_folders(client, current_folder_id):
    folder, items = get_folder_contents(client, current_folder_id)
    
    # Create breadcrumbs (path to the current folder, excluding root and current folder itself)
    path_parts = []
    ancestor_tracer = folder # Start with the current folder object
    
    while True:
        # Check if the current tracer has a 'parent' attribute and it's populated
        if not hasattr(ancestor_tracer, 'parent') or not ancestor_tracer.parent:
            # This means ancestor_tracer is root, or its parent info is not available
            break 
        
        parent_ref = ancestor_tracer.parent  # This is a BoxMiniFolder-like object for the parent

        if parent_ref.id == "0":
            # The parent is the root folder. Stop, as "Root" is handled by a separate button.
            break 
        
        try:
            # Fetch the full parent folder object to get its name and its own parent (for the next step up)
            parent_full_obj = client.folder(parent_ref.id).get(fields=["name", "parent"])
        except Exception as e:
            st.warning(f"Could not retrieve parent folder details for ID {parent_ref.id}: {e}")
            break # Stop breadcrumb generation if a parent folder is inaccessible

        path_parts.insert(0, {"id": parent_full_obj.id, "name": parent_full_obj.name})
        
        ancestor_tracer = parent_full_obj  # Move one level up in the hierarchy
    
    # Display breadcrumbs
    # Number of columns: 1 for "Root" + one for each part in path_parts
    breadcrumb_cols = st.columns(len(path_parts) + 1) 
    
    # "Root" button always navigates to folder "0"
    breadcrumb_cols[0].button("Root", key="nav_root_button", 
                              on_click=lambda: st.session_state.update({"current_folder": "0"}))
    
    for i, part in enumerate(path_parts):
        # Use a unique key for each breadcrumb button
        breadcrumb_cols[i+1].button(
            part["name"], 
            key=f"nav_breadcrumb_{part['id']}", 
            on_click=lambda folder_id_val=part["id"]: st.session_state.update({"current_folder": folder_id_val})
        )
    
    # Display current folder name
    st.subheader(f"Folder: {folder.name}")
    
    # Display folder contents header
    header_cols = st.columns([0.7, 0.2, 0.1])
    header_cols[0].write("Name")
    header_cols[1].write("Type")
    header_cols[2].write("Select")
        
    for item in items:
        item_cols = st.columns([0.7, 0.2, 0.1])
        
        if item.type == "folder":
            item_cols[0].write(f"📁 {item.name}")
            item_cols[1].write("Folder")
            # Use unique key for "Open" button
            item_cols[2].button("Open", key=f"open_folder_{item.id}", 
                                on_click=lambda folder_id_val=item.id: st.session_state.update({"current_folder": folder_id_val}))
        else: # It's a file
            item_cols[0].write(f"📄 {item.name}")
            item_cols[1].write(item.type.capitalize()) # 'file' -> 'File'
            
            file_extension = item.name.split('.')[-1].lower() if '.' in item.name else ''
            
            # Use unique keys for checkboxes
            if file_extension == 'docx':
                selected = item_cols[2].checkbox("", key=f"select_template_{item.id}", 
                                                  value=st.session_state.get('template_file_id') == item.id)
                if selected:
                    st.session_state['template_file_id'] = item.id
                    st.session_state['template_file_name'] = item.name
                    st.session_state['template_file_type'] = 'docx'
                # If unselected and it was the current one, logic to clear (optional, original didn't have this)
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
        pattern = r"\{\{(.*?)(\||\}\})" # Original pattern might miss fields like {{TableStart:FieldName}}
        # A more general pattern for {{field_name}} or {{TableStart:Table}} etc.
        # For simple merge fields like {{FieldName}}, the original is fine.
        # Let's assume original pattern is sufficient for Conga syntax intended.
        for para in document.paragraphs:
            matches = re.findall(pattern, para.text)
            for match in matches:
                fields.add(match[0].strip())
        # Also check tables
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
        # Assuming the CSV file itself contains data, and columns are fields
        # If CSV contains a query string, this logic needs to change
        df = pd.read_csv(io.BytesIO(file_content))
        return list(df.columns)
    else:  # txt (presumably SOQL query)
        text = file_content.decode("utf-8")
        # This regex is basic. SOQL can be complex.
        # SELECT Field1, Field2 FROM Object WHERE ...
        # It might miss fields in subqueries or relationships.
        match = re.search(r"select\s+(.+?)\s+from", text, re.IGNORECASE | re.DOTALL)
        if match:
            fields_segment = match.group(1)
            # Simple split by comma. Might not handle "Field, MAX(Amount)" or "Account.Name" correctly if just Account is needed.
            # For "Account.Name", "Account.Name" is one field.
            # This needs to be robust if queries are complex, e.g. using regex to find valid field names
            raw_fields = fields_segment.split(",")
            return [f.strip() for f in raw_fields if f.strip()]
        return []

def flatten_json(y, prefix=''):
    out = {}
    def flatten(x, name=''):
        if isinstance(x, dict):
            for a in x:
                flatten(x[a], f'{name}{a}.')
        elif isinstance(x, list):
            # If list contains dicts, you might want to handle them.
            # This flattens list items by index: list.0.field, list.1.field
            # For schema, sometimes lists represent choices or repeated structures not data instances.
            # Depending on schema structure, this might need adjustment.
            # For now, assuming this generic flattening is intended.
            for i, a in enumerate(x):
                flatten(a, f'{name}{i}.')
        else:
            out[name[:-1]] = str(x) # name[:-1] to remove trailing dot
    flatten(y, prefix)
    return out

def chunk_dict(d, chunk_size=50): # Default chunk size for prompt
    items = list(d.items())
    for i in range(0, len(items), chunk_size):
        yield dict(items[i:i + chunk_size])

def generate_prompt(merge_fields, conga_fields, schema_chunk):
    # Convert lists to string representations for the prompt
    merge_fields_str = ", ".join(merge_fields)
    conga_fields_str = ", ".join(conga_fields)
    
    schema_text = "\n".join([f"- {k}: (example value: {v})" for k, v in schema_chunk.items()]) # Show example values may help AI
    
    prompt = (
        "You are an AI assistant helping map fields from a Conga document generation system to a Box Doc Gen system, "
        "which uses a Salesforce schema.\n\n"
        "Conga Template Merge Fields:\n"
        f"{merge_fields_str}\n\n"
        "Fields from Conga Query (likely Salesforce source fields):\n"
        f"{conga_fields_str}\n\n"
        "Partial Salesforce Schema (Path: Example Value from a record):\n" # Clarify schema representation
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

def call_box_ai(prompt, file_ids, developer_token):
    url = "https://api.box.com/2.0/ai/ask"
    headers = {
        "Authorization": f"Bearer {developer_token}",
        "Content-Type": "application/json"
    }
    # Ensure file_ids being passed are for the relevant context if Box AI uses them.
    # The prompt already contains extracted info, so file_ids might be for Box AI's broader context/logging.
    # If files are large, ensure this is the intended way to use Box AI (passing content vs. IDs).
    data = {
        "mode": "text_gen", # Explicitly set mode if available/needed by Box AI
        "prompt": prompt,
        "items": [{"type": "file", "id": file_id, "content_type": "text/plain"} for file_id in file_ids if file_id] # Assuming text context
        # If Box AI can read docx, csv, json directly, content_type could be more specific, or Box infers.
        # The prompt itself contains the necessary data, so items might be optional or for grounding.
    }
    
    # If 'items' are not actually used by Box AI for this prompt (since data is in prompt_text),
    # they could be omitted to simplify or if they cause issues.
    # For this use case, prompt has all info. Let's try without 'items' if it's problematic.
    # For now, keeping it as per original.

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        # Assuming the answer is directly in response.json()["answer"]
        return response.json().get("answer", "")
    else:
        st.error(f"Box AI request failed: {response.status_code} — {response.text}")
        return None

def convert_response_to_df(text):
    if not text or not isinstance(text, str):
        st.warning("AI response was empty or not a string.")
        return pd.DataFrame()

    lines = text.strip().splitlines()
    
    # Find header row, robustly
    header_idx = -1
    for i, line in enumerate(lines):
        if "CongaField" in line and "BoxField" in line: # Check for key column names
            header_idx = i
            break
    
    if header_idx == -1:
        st.warning("CSV header (CongaField,BoxField,FieldType) not found in AI response.")
        st.text_area("AI Response", text, height=150)
        return pd.DataFrame()

    header = [h.strip() for h in lines[header_idx].split(",")]
    data_lines = lines[header_idx+1:]
    
    data = []
    for line in data_lines:
        split_line = [val.strip() for val in line.split(",")]
        if len(split_line) == len(header): # Ensure correct number of columns
            data.append(split_line)
        elif len(split_line) > len(header): # If values contain commas
             # Try to reconstruct, assuming CongaField is first, BoxField second, rest is FieldType
            if len(split_line) >= 2: # At least CongaField and BoxField parts
                data.append([split_line[0], split_line[1], ",".join(split_line[2:])])
            else:
                st.warning(f"Skipping malformed CSV line: {line}")
        else:
            st.warning(f"Skipping malformed CSV line (not enough columns): {line}")

    if not data:
        st.warning("No valid data rows found in AI response after header.")
        st.text_area("AI Response", text, height=150)
        return pd.DataFrame()
        
    return pd.DataFrame(data, columns=header)


# Initialize session state
if 'current_folder' not in st.session_state:
    st.session_state['current_folder'] = "0"  # Root folder

st.title("Conga Template to Box Doc Gen Mapper")

# Sidebar for Box navigation
with st.sidebar:
    st.header("Box File Browser")
    
    try:
        client = get_box_client()
        # Pass client and current_folder_id to navigate_folders
        navigate_folders(client, st.session_state['current_folder']) 
        
        st.subheader("Selected Files")
        # Display selected files (robustly checking if keys exist)
        st.write(f"Template: {st.session_state.get('template_file_name', 'Not selected')}")
        st.write(f"Query: {st.session_state.get('query_file_name', 'Not selected')}")
        st.write(f"Schema: {st.session_state.get('schema_file_name', 'Not selected')}")
            
    except Exception as e:
        st.error(f"Error connecting to Box or browsing folders: {str(e)}")
        st.caption("Please check your Box credentials in secrets and network.")

# Main area for processing
if st.button("Generate Field Mapping"):
    if not all([st.session_state.get(key) for key in ['template_file_id', 'query_file_id', 'schema_file_id']]):
        st.warning("Please select a DOCX template, a TXT/CSV query file, and a JSON schema file from Box.")
        st.stop()

    st.info("Processing selected files and generating mapping...")
    
    try:
        # Ensure client is available from sidebar try-except block
        # If client failed, this button click should ideally not proceed or re-check.
        # Assuming client is valid if we reach here.
        if 'client' not in locals() or client is None: # Defensive check
             client = get_box_client()


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
            st.success(f"Extracted {len(conga_fields)} fields from Conga query: {', '.join(conga_fields[:5])}{'...' if len(conga_fields) > 5 else ''}")
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
        
        # File IDs for Box AI context (if used by the AI model for grounding)
        # The prompt already contains extracted data, so these might be optional for the AI call.
        context_file_ids = [
            st.session_state.get('template_file_id'),
            st.session_state.get('query_file_id'),
            st.session_state.get('schema_file_id')
        ]
        context_file_ids = [fid for fid in context_file_ids if fid] # Filter out None values

        full_mapping_df = pd.DataFrame()
        
        if not merge_fields:
            st.error("Cannot generate mapping without merge fields from the template.")
            st.stop()

        # Using a smaller chunk size for schema in prompt as it can be verbose.
        # The Box AI token limit for the prompt needs to be considered.
        # Max schema fields per call can be adjusted.
        schema_chunk_size = 30 # Adjust based on typical path/value length and token limits

        for i, schema_chunk in enumerate(chunk_dict(flat_schema, chunk_size=schema_chunk_size)):
            st.info(f"Calling Box AI (part {i+1} of schema)...")
            prompt_text = generate_prompt(merge_fields, conga_fields, schema_chunk)
            
            # For debugging the prompt:
            # with st.expander(f"Prompt for Schema Chunk {i+1}"):
            #    st.text(prompt_text)

            ai_response_text = call_box_ai(prompt_text, context_file_ids, st.secrets["box"]["BOX_DEVELOPER_TOKEN"])
            
            if ai_response_text:
                mapping_df_chunk = convert_response_to_df(ai_response_text)
                if not mapping_df_chunk.empty:
                    full_mapping_df = pd.concat([full_mapping_df, mapping_df_chunk], ignore_index=True)
            else:
                st.warning(f"No response or error from Box AI for schema chunk {i+1}.")
        
        if not full_mapping_df.empty:
            # Post-processing: Deduplicate and potentially rank/select best matches if AI gives multiple for one CongaField
            full_mapping_df.drop_duplicates(subset=['CongaField', 'BoxField'], inplace=True)
            # A more sophisticated merge/ranking could be added here if AI provides multiple options per CongaField across chunks.
            
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
        # For debugging, show more details if possible
        import traceback
        st.text(traceback.format_exc())
