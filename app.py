import streamlit as st
from docx import Document
import pandas as pd
import json
import re
import io
import requests
from boxsdk import OAuth2, Client
from boxsdk.object.file import File

st.set_page_config(page_title="Conga to Box Doc Gen", layout="centered")

# Box API configuration
def get_box_client():
    client_id = st.secrets["BOX_CLIENT_ID"]
    client_secret = st.secrets["BOX_CLIENT_SECRET"]
    developer_token = st.secrets["BOX_DEVELOPER_TOKEN"]
    
    oauth = OAuth2(
        client_id=client_id,
        client_secret=client_secret,
        access_token=developer_token,
    )
    client = Client(oauth)
    return client

def get_folder_contents(client, folder_id):
    folder = client.folder(folder_id).get()
    items = client.folder(folder_id).get_items()
    return folder, items

def navigate_folders(client, current_folder_id):
    folder, items = get_folder_contents(client, current_folder_id)
    
    # Create breadcrumbs
    path_parts = []
    parent = folder
    while parent.id != "0":
        parent = client.folder(parent.id).get(fields=["name", "parent"])
        path_parts.insert(0, {"id": parent.id, "name": parent.name})
    
    # Display breadcrumbs
    cols = st.columns(len(path_parts) + 1)
    cols[0].button("Root", key=f"nav_0", on_click=lambda: st.session_state.update({"current_folder": "0"}))
    
    for i, part in enumerate(path_parts):
        cols[i+1].button(part["name"], key=f"nav_{part['id']}", 
                         on_click=lambda id=part["id"]: st.session_state.update({"current_folder": id}))
    
    # Display current folder name
    st.subheader(f"Folder: {folder.name}")
    
    # Display folder contents
    cols = st.columns([0.7, 0.2, 0.1])
    cols[0].write("Name")
    cols[1].write("Type")
    cols[2].write("Select")
    
    selected_files = {}
    
    for item in items:
        cols = st.columns([0.7, 0.2, 0.1])
        
        if item.type == "folder":
            cols[0].write(f"📁 {item.name}")
            cols[1].write("Folder")
            cols[2].button("Open", key=f"open_{item.id}", 
                          on_click=lambda id=item.id: st.session_state.update({"current_folder": id}))
        else:
            cols[0].write(f"📄 {item.name}")
            cols[1].write(item.type.capitalize())
            
            # Allow selection of files based on type
            file_extension = item.name.split('.')[-1].lower() if '.' in item.name else ''
            
            if file_extension == 'docx':
                selected = cols[2].checkbox("", key=f"select_template_{item.id}", 
                                          value=st.session_state.get('template_file_id') == item.id)
                if selected:
                    st.session_state['template_file_id'] = item.id
                    st.session_state['template_file_name'] = item.name
                    st.session_state['template_file_type'] = 'docx'
            
            elif file_extension in ['txt', 'csv']:
                selected = cols[2].checkbox("", key=f"select_query_{item.id}", 
                                          value=st.session_state.get('query_file_id') == item.id)
                if selected:
                    st.session_state['query_file_id'] = item.id
                    st.session_state['query_file_name'] = item.name
                    st.session_state['query_file_type'] = file_extension
            
            elif file_extension == 'json':
                selected = cols[2].checkbox("", key=f"select_schema_{item.id}", 
                                          value=st.session_state.get('schema_file_id') == item.id)
                if selected:
                    st.session_state['schema_file_id'] = item.id
                    st.session_state['schema_file_name'] = item.name
                    st.session_state['schema_file_type'] = 'json'

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
        return list(fields)

def parse_conga_query(file_content, file_type):
    if file_type == 'csv':
        df = pd.read_csv(io.BytesIO(file_content))
        return list(df.columns)
    else:  # txt
        text = file_content.decode("utf-8")
        match = re.search(r"select (.+?) from", text, re.IGNORECASE)
        if match:
            fields = match.group(1)
            return [f.strip() for f in fields.split(",")]
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
    schema_text = "\n".join([f"{k}: {v}" for k, v in schema_chunk.items()])
    prompt = (
        "You are assisting in converting a Conga template to Box Doc Gen.\n\n"
        "Here are the merge fields from the Conga template:\n"
        f"{merge_fields}\n\n"
        "These are the fields referenced in the Conga query:\n"
        f"{conga_fields}\n\n"
        "And here is a portion of the Box-Salesforce schema:\n"
        f"{schema_text}\n\n"
        "Please map each Conga merge field to the most relevant field in the schema.\n"
        "Respond in CSV format with the following columns: CongaField,BoxField,FieldType."
    )
    return prompt

def call_box_ai(prompt, file_ids, developer_token):
    url = "https://api.box.com/2.0/ai/ask"
    headers = {
        "Authorization": f"Bearer {developer_token}",
        "Content-Type": "application/json"
    }
    data = {
        "prompt": prompt,
        "items": [{"type": "file", "id": file_id} for file_id in file_ids if file_id]
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        return response.json().get("answer", "")
    else:
        st.error(f"Box AI request failed: {response.status_code} — {response.text}")
        return None

def convert_response_to_df(text):
    lines = text.strip().splitlines()
    data = [line.split(",") for line in lines if ',' in line]
    return pd.DataFrame(data[1:], columns=data[0]) if data else pd.DataFrame()

# Initialize session state
if 'current_folder' not in st.session_state:
    st.session_state['current_folder'] = "0"  # Root folder

st.title("Conga Template to Box Doc Gen Mapper")

# Sidebar for Box navigation
with st.sidebar:
    st.header("Box File Browser")
    
    try:
        client = get_box_client()
        navigate_folders(client, st.session_state['current_folder'])
        
        # Display selected files
        st.subheader("Selected Files")
        
        if 'template_file_id' in st.session_state:
            st.write(f"Template: {st.session_state.get('template_file_name', '')}")
        else:
            st.write("Template: Not selected")
            
        if 'query_file_id' in st.session_state:
            st.write(f"Query: {st.session_state.get('query_file_name', '')}")
        else:
            st.write("Query: Not selected")
            
        if 'schema_file_id' in st.session_state:
            st.write(f"Schema: {st.session_state.get('schema_file_name', '')}")
        else:
            st.write("Schema: Not selected")
            
    except Exception as e:
        st.error(f"Error connecting to Box: {str(e)}")
        st.write("Please check your Box credentials.")

# Main area for processing
if st.button("Generate Field Mapping"):
    if not (st.session_state.get('template_file_id') and 
            st.session_state.get('query_file_id') and 
            st.session_state.get('schema_file_id')):
        st.warning("Please select all required files from Box.")
        st.stop()

    st.info("Processing template and inputs...")
    
    try:
        # Download files from Box
        template_content = download_box_file(client, st.session_state['template_file_id'])
        query_content = download_box_file(client, st.session_state['query_file_id'])
        schema_content = download_box_file(client, st.session_state['schema_file_id'])
        
        # Process the files
        merge_fields = extract_merge_fields(template_content)
        st.success(f"Extracted {len(merge_fields)} merge fields from template.")
        
        conga_fields = parse_conga_query(query_content, st.session_state['query_file_type'])
        st.success(f"Extracted {len(conga_fields)} fields from Conga query.")
        
        schema = json.loads(schema_content.decode('utf-8'))
        flat_schema = flatten_json(schema)
        st.success(f"Flattened schema into {len(flat_schema)} fields.")
        
        # Pass all file IDs to Box AI
        file_ids = [
            st.session_state['template_file_id'],
            st.session_state['query_file_id'],
            st.session_state['schema_file_id']
        ]
        
        full_mapping_df = pd.DataFrame()
        for chunk in chunk_dict(flat_schema, chunk_size=50):
            prompt = generate_prompt(merge_fields, conga_fields, chunk)
            ai_text = call_box_ai(prompt, file_ids, st.secrets["BOX_DEVELOPER_TOKEN"])
            if ai_text:
                mapping_df = convert_response_to_df(ai_text)
                full_mapping_df = pd.concat([full_mapping_df, mapping_df], ignore_index=True)
        
        if not full_mapping_df.empty:
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
            st.warning("No valid mapping was returned by Box AI.")
    
    except Exception as e:
        st.error(f"Error processing files: {str(e)}")
