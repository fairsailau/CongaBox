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
            core_field = re.split(r'\s*(?:\\@|\||&)', field, 1)[0] 
            cleaned_fields.add(core_field.strip())
        
        if len(raw_fields) != len(cleaned_fields) or any(r != c for r, c in zip(sorted(list(raw_fields)), sorted(list(cleaned_fields)))):
            st.info(f"Cleaned merge fields. Original unique tags found: {len(raw_fields)}. Core fields for AI: {len(cleaned_fields)}")
        return list(cleaned_fields)

def parse_conga_query(file_content, file_type):
    sobject_to_fields_map = {}
    all_unique_fields = set()
    all_unique_sobjects = set()

    def extract_from_soql_list(query_text_list):
        processed_queries_count = 0
        for query_text_item in query_text_list:
            query_text_item = str(query_text_item).strip()
            if not query_text_item: continue
            
            from_match = re.search(r"from\s+([a-zA-Z0-9_]+(?:__c|__r)?)\b", query_text_item, re.IGNORECASE)
            current_sobject_from_query = from_match.group(1) if from_match else None # Renamed
            
            if current_sobject_from_query: # Use the renamed variable
                all_unique_sobjects.add(current_sobject_from_query)
                if current_sobject_from_query not in sobject_to_fields_map:
                    sobject_to_fields_map[current_sobject_from_query] = set()

            if "select " in query_text_item.lower() and " from " in query_text_item.lower():
                processed_queries_count +=1
                select_match = re.search(r"select\s+(.+?)\s+from", query_text_item, re.IGNORECASE | re.DOTALL)
                if select_match:
                    fields_segment = select_match.group(1)
                    current_query_fields_list = re.findall(r"([\w\._()]+(?:\s+AS\s+[\w\._]+)?)(?:\s*,|\s+FROM)", fields_segment + " FROM", re.IGNORECASE | re.DOTALL)
                    
                    cleaned_query_fields_set = {f.strip() for f in current_query_fields_list if f.strip()} # Renamed
                    all_unique_fields.update(cleaned_query_fields_set)
                    if current_sobject_from_query: # Use the renamed variable
                        sobject_to_fields_map[current_sob_from_query].update(cleaned_query_fields_set)
        return processed_queries_count

    queries_to_parse = []
    if file_type == 'csv':
        try:
            # Attempt to read the first column as a series of queries
            df = pd.read_csv(io.BytesIO(file_content), header=None, usecols=[0], squeeze=True, skip_blank_lines=True)
            if isinstance(df, pd.Series):
                queries_to_parse = df.dropna().astype(str).tolist()
            elif isinstance(df, pd.DataFrame) and not df.empty: # Should ideally be handled by squeeze=True
                 queries_to_parse = df.iloc[:,0].dropna().astype(str).tolist()
            
            if queries_to_parse:
                 st.info(f"CSV: Processing {len(queries_to_parse)} lines from the first column as potential SOQL queries.")
            else: # Fallback if squeeze didn't work or CSV not as expected
                st.warning("CSV query file could not be parsed as a list of queries. Trying column headers as fallback.")
                # Reread to get actual headers if the squeeze approach failed
                df_headers = pd.read_csv(io.BytesIO(file_content), nrows=0) 
                all_unique_fields.update([str(col).strip() for col in df_headers.columns])
                st.info(f"CSV (Fallback): Using column headers as query fields: {list(all_unique_fields)}")
                if all_unique_fields:
                    sobject_to_fields_map["UnknownFromCSVHeaders"] = set(all_unique_fields)
                # Important: Return here for this fallback case
                final_map_for_fallback = {k: sorted(list(v)) for k, v in sobject_to_fields_map.items()}
                return final_map_for_fallback, all_unique_fields, all_unique_sobjects


        except Exception as e:
            st.error(f"Error parsing CSV query file: {e}. Attempting fallback to TXT parsing.")
            try: 
                return parse_conga_query(file_content, 'txt') # Recursive call
            except Exception as e_txt:
                 st.error(f"Fallback to TXT parsing also failed: {e_txt}")
                 return {}, set(), set() # Return empty structures on complete failure
    
    elif file_type == 'txt':
        text_content = file_content.decode("utf-8")
        queries_to_parse = [q.strip() for q in re.split(r';\s*\n?|\n\s{2,}\n?|\n\s*$', text_content, flags=re.MULTILINE) if q.strip()]
        if queries_to_parse: st.info(f"TXT: Found {len(queries_to_parse)} potential SOQL queries.")
    else:
        st.error(f"Unsupported query file type: {file_type}")
        return {}, set(), set()

    processed_count = 0
    if queries_to_parse: # Ensure there are queries before calling extract
        processed_count = extract_from_soql_list(queries_to_parse)
    
    if processed_count > 0:
        st.success(f"{file_type.upper()}: Processed {processed_count} SOQL queries. Found {len(all_unique_fields)} unique fields across {len(all_unique_sobjects)} SObjects.")
    elif queries_to_parse: 
        st.warning(f"{file_type.upper()}: No SOQL queries were successfully processed from {len(queries_to_parse)} candidates.")
    else: 
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
            if mf_lower.startswith(sobj_clean_name) or \
               mf_lower.replace("_", "").startswith(sobj_clean_name.replace("_","")):
                if sobj not in [s.split(" ")[0] for s in possible_sources]:
                    possible_sources.append(f"{sobj} (prefix match)")
            else: 
                for q_field in fields:
                    q_field_core = q_field.split('.')[-1].lower() 
                    if mf_lower == q_field_core or mf_lower in q_field_core or q_field_core in mf_lower :
                        if sobj not in [s.split(" ")[0] for s in possible_sources]:
                             possible_sources.append(f"{sobj} (related query field: {q_field})")
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
    # Check if model_id is not None and not the string "None" (if it might come from selectbox directly)
    # And ensure it's not the value representing the default choice if your dict has a specific key for default
    if model_id: 
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
        if all(expected_header.lower() in line.lower() for expected_header in expected_headers_check):
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
        
        row_data = {col: "" for col in final_columns} 

        # Try to map based on expected column names, being flexible with AI's header naming
        mapped_successfully_by_header_name = False
        temp_ai_headers_lower = [h.lower() for h in header_from_ai_raw]

        for i, expected_col_name in enumerate(final_columns):
            try:
                # Find index of expected_col_name in AI's headers (case-insensitive)
                ai_col_idx = temp_ai_headers_lower.index(expected_col_name.lower())
                if ai_col_idx < len(split_line_values):
                    # If it's the last expected column and there are more split values, join them
                    if expected_col_name == final_columns[-1] and ai_col_idx < len(split_line_values) and len(split_line_values) > len(header_from_ai_raw):
                        row_data[expected_col_name] = ",".join(split_line_values[ai_col_idx:])
                    else:
                        row_data[expected_col_name] = split_line_values[ai_col_idx]
                    mapped_successfully_by_header_name = True 
            except ValueError: # Header not found in AI's list
                pass # Keep default empty string for this expected_col_name

        # If mapping by header name didn't populate CongaField (crucial field), try positional.
        if not row_data["CongaField"]:
            if len(split_line_values) >= 1: row_data["CongaField"] = split_line_values[0]
            if len(split_line_values) >= 2: row_data["BoxField"] = split_line_values[1]
            if len(split_line_values) >= 3: row_data["FieldType"] = ",".join(split_line_values[2:])
        
        if row_data["CongaField"]: # Only add if CongaField is populated
            processed_data_list.append(row_data)
        elif any(row_data.values()): 
            st.info(f"Skipping row with empty CongaField but other data: {row_data}")
            
    if not processed_data_list:
        st.warning("No valid data rows extracted from AI response.")
        st.text_area("Processed AI Text (for debugging)", csv_text, height=100)
        st.text_area("Original AI Response (for debugging)", text, height=150)
        return pd.DataFrame(columns=final_columns)
        
    return pd.DataFrame(processed_data_list, columns=final_columns)


# Main processing block (pasted from previous, ensure it's complete)
if st.button("Generate Field Mapping", key="generate_mapping_button"): # Added key to button
    if 'client' not in locals() or client is None: 
        client = get_box_client() 
        if client is None:
            st.error("Box client could not be initialized. Please resolve errors in the sidebar and try again.")
            st.stop()

    if not all([st.session_state.get(k) for k in ['template_file_id', 'query_file_id', 'schema_file_id']]):
        st.warning("Please select DOCX template, TXT/CSV query, and JSON schema files."); st.stop()

    # Retrieve selected_model_id from session_state using the key from the selectbox
    chosen_model_display_name_from_state = st.session_state.get("ai_model_selector", list(BOX_AI_MODELS_FOR_SELECTBOX.keys())[0])
    final_selected_model_id = BOX_AI_MODELS_FOR_SELECTBOX.get(chosen_model_display_name_from_state)


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
        sobject_to_fields_map, all_query_fields, all_query_sobjects = parse_conga_query(query_content, st.session_state['query_file_type'])
        
        progress_bar_main.progress(0.4, text="Validating schema file ID...")
        schema_file_id_for_ai = st.session_state.get('schema_file_id')
        if not schema_file_id_for_ai: st.error("Schema file ID missing for AI call."); st.stop()
        st.success(f"Schema file ID {schema_file_id_for_ai} will be used for AI context.")

        if not merge_fields: st.error("No merge fields to map."); st.stop()

        full_mapping_df = pd.DataFrame()
        
        progress_bar_main.progress(0.5, text="Calling Box AI...")
        # Pass the more detailed sobject_to_fields_map and all_query_sobjects
        prompt_text = generate_prompt_single_call(merge_fields or [], sobject_to_fields_map, all_query_sobjects or [])
        
        ai_response_text = call_box_ai(
            prompt_text, 
            schema_file_id_for_ai, 
            st.secrets["box"]["BOX_DEVELOPER_TOKEN"], 
            final_selected_model_id # Pass the chosen model ID
        )
        
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
            for col in expected_cols: # Ensure all expected columns exist
                if col not in full_mapping_df.columns:
                    full_mapping_df[col] = "" 
            full_mapping_df = full_mapping_df[expected_cols] # Reorder/select to ensure consistent structure

            full_mapping_df.drop_duplicates(subset=['CongaField', 'BoxField'], inplace=True, keep='first')
            st.subheader("Generated Field Mapping")
            st.dataframe(full_mapping_df)
            csv_buffer = io.StringIO()
            full_mapping_df.to_csv(csv_buffer, index=False)
            st.download_button("Download Mapping CSV", csv_buffer.getvalue(), "field_mapping.csv", "text/csv")
        else:
            st.warning("No valid field mappings generated by Box AI.")
    
    except Exception as e:
        st.error(f"An error occurred during processing: {str(e)}")
        import traceback
        st.text(traceback.format_exc())
        if 'progress_bar_main' in locals() and progress_bar_main is not None: progress_bar_main.empty()
