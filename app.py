
import streamlit as st
from docx import Document
import pandas as pd
import json
import re
import io
import requests

st.set_page_config(page_title="Conga to Box Doc Gen", layout="centered")

def extract_merge_fields(docx_file):
    document = Document(docx_file)
    fields = set()
    pattern = r"\{\{(.*?)(\||\}\})"
    for para in document.paragraphs:
        matches = re.findall(pattern, para.text)
        for match in matches:
            fields.add(match[0].strip())
    return list(fields)

def parse_conga_query(file):
    if file.name.endswith(".csv"):
        df = pd.read_csv(file)
        return list(df.columns)
    else:
        text = file.read().decode("utf-8")
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

def call_box_ai(prompt, developer_token):
    url = "https://api.box.com/2.0/ai/prompts"
    headers = {
        "Authorization": f"Bearer {developer_token}",
        "Content-Type": "application/json"
    }
    data = {
        "prompt": prompt,
        "model": "gpt-4"
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        return response.json().get("text", "")
    else:
        st.error(f"Box AI request failed: {response.status_code}")
        return None

def convert_response_to_df(text):
    lines = text.strip().splitlines()
    data = [line.split(",") for line in lines if ',' in line]
    return pd.DataFrame(data[1:], columns=data[0]) if data else pd.DataFrame()

st.title("Conga Template to Box Doc Gen Mapper")

docx_file = st.file_uploader("Upload Conga Template (.docx)", type=["docx"])
query_file = st.file_uploader("Upload Conga Query (.txt or .csv)", type=["txt", "csv"])
json_file = st.file_uploader("Upload Box-Salesforce Schema (.json)", type=["json"])

box_token = st.secrets["BOX_DEVELOPER_TOKEN"]

if st.button("Generate Field Mapping"):
    if not (docx_file and query_file and json_file):
        st.warning("Please upload all required files.")
        st.stop()

    st.info("Processing template and inputs...")

    merge_fields = extract_merge_fields(docx_file)
    st.success(f"Extracted {len(merge_fields)} merge fields from template.")

    conga_fields = parse_conga_query(query_file)
    st.success(f"Extracted {len(conga_fields)} fields from Conga query.")

    schema = json.load(json_file)
    flat_schema = flatten_json(schema)
    st.success(f"Flattened schema into {len(flat_schema)} fields.")

    full_mapping_df = pd.DataFrame()
    for chunk in chunk_dict(flat_schema, chunk_size=50):
        prompt = generate_prompt(merge_fields, conga_fields, chunk)
        ai_text = call_box_ai(prompt, box_token)
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
