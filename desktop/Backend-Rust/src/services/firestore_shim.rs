//! Firestore-REST-over-MongoDB shim.
//!
//! Strategy (mirrors the Python backend's `mongo_firestore.py`): keep the
//! upstream Firestore methods in `firestore.rs` UNCHANGED and redirect their REST
//! calls to MongoDB at a single seam, so upstream merges stay clean and nothing
//! actually talks to Google Firestore.
//!
//! This module holds the **typed-value converter** — the translation between
//! Firestore's REST JSON value encoding and BSON. The request dispatcher and the
//! `firestore.rs` seam wiring build on top of it.
//!
//! Firestore REST encodes every field as a single-key "Value" object:
//!   {"stringValue": "x"} {"integerValue": "5"} {"doubleValue": 1.5}
//!   {"booleanValue": true} {"timestampValue": "2026-..Z"} {"nullValue": null}
//!   {"mapValue": {"fields": {k: Value}}} {"arrayValue": {"values": [Value]}}
//! (integerValue is a *string* in REST JSON.) A document body is {"fields": {k: Value}}.
#![allow(dead_code)]

use bson::{Bson, Document};
use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value};

/// Convert one Firestore REST "Value" object into BSON.
pub fn value_to_bson(v: &Value) -> Bson {
    let obj = match v.as_object() {
        Some(o) => o,
        None => return Bson::Null,
    };
    if let Some(s) = obj.get("stringValue").and_then(|x| x.as_str()) {
        return Bson::String(s.to_string());
    }
    if let Some(i) = obj.get("integerValue") {
        // REST encodes integers as strings, but tolerate a raw number too.
        let parsed = i
            .as_str()
            .and_then(|s| s.parse::<i64>().ok())
            .or_else(|| i.as_i64());
        return Bson::Int64(parsed.unwrap_or(0));
    }
    if let Some(d) = obj.get("doubleValue") {
        return Bson::Double(d.as_f64().unwrap_or(0.0));
    }
    if let Some(b) = obj.get("booleanValue") {
        return Bson::Boolean(b.as_bool().unwrap_or(false));
    }
    if let Some(t) = obj.get("timestampValue").and_then(|x| x.as_str()) {
        return match DateTime::parse_from_rfc3339(t) {
            Ok(dt) => Bson::DateTime(bson::DateTime::from_chrono(dt.with_timezone(&Utc))),
            Err(_) => Bson::String(t.to_string()),
        };
    }
    if obj.contains_key("nullValue") {
        return Bson::Null;
    }
    if let Some(m) = obj.get("mapValue") {
        let fields = m.get("fields").and_then(|f| f.as_object());
        return Bson::Document(fields_to_document(fields.unwrap_or(&Map::new())));
    }
    if let Some(a) = obj.get("arrayValue") {
        let values = a.get("values").and_then(|v| v.as_array());
        let arr = values
            .map(|vs| vs.iter().map(value_to_bson).collect())
            .unwrap_or_default();
        return Bson::Array(arr);
    }
    Bson::Null
}

/// Convert a Firestore REST `fields` map into a BSON document.
pub fn fields_to_document(fields: &Map<String, Value>) -> Document {
    let mut doc = Document::new();
    for (k, v) in fields {
        doc.insert(k.clone(), value_to_bson(v));
    }
    doc
}

/// Convert a BSON value into a Firestore REST "Value" object.
pub fn bson_to_value(b: &Bson) -> Value {
    match b {
        Bson::String(s) => json!({ "stringValue": s }),
        Bson::Int32(i) => json!({ "integerValue": i.to_string() }),
        Bson::Int64(i) => json!({ "integerValue": i.to_string() }),
        Bson::Double(d) => json!({ "doubleValue": d }),
        Bson::Boolean(b) => json!({ "booleanValue": b }),
        Bson::DateTime(dt) => json!({ "timestampValue": dt.to_chrono().to_rfc3339() }),
        Bson::Null => json!({ "nullValue": null }),
        Bson::Document(d) => json!({ "mapValue": { "fields": document_to_fields(d) } }),
        Bson::Array(arr) => {
            json!({ "arrayValue": { "values": arr.iter().map(bson_to_value).collect::<Vec<_>>() } })
        }
        // Anything else (rarely used by this backend) degrades to its string form.
        other => json!({ "stringValue": other.to_string() }),
    }
}

/// Convert a BSON document into a Firestore REST `fields` object, skipping the
/// shim's reserved keys (`_id`/`_p`/`_k`).
pub fn document_to_fields(doc: &Document) -> Value {
    let mut fields = Map::new();
    for (k, v) in doc {
        if k == "_id" || k == "_p" || k == "_k" {
            continue;
        }
        fields.insert(k.clone(), bson_to_value(v));
    }
    Value::Object(fields)
}

// ===========================================================================
// Request dispatcher: Firestore REST operation -> MongoStore
// ===========================================================================

use bson::doc;
use super::mongo_store::MongoStore;

const DOCS_MARKER: &str = "/databases/(default)/documents";

/// True for the Firestore REST URLs the shim intercepts (vs. GCE compute /
/// identitytoolkit URLs, which pass through to real GCP).
pub fn is_firestore_url(url: &str) -> bool {
    url.contains("firestore.googleapis.com") && url.contains(DOCS_MARKER)
}

/// Extract the resource path (everything after `/databases/(default)/documents`,
/// keeping any `:runQuery`/`:commit` suffix, no leading slash) and the
/// `updateMask.fieldPaths` values from a Firestore REST URL.
pub fn parse_firestore_url(url: &str) -> Option<(String, Vec<String>)> {
    let after = url.split(DOCS_MARKER).nth(1)?;
    let (path_part, query) = match after.split_once('?') {
        Some((p, q)) => (p, Some(q)),
        None => (after, None),
    };
    let resource = path_part.trim_start_matches('/').to_string();
    let mut mask = Vec::new();
    if let Some(q) = query {
        for kv in q.split('&') {
            if let Some(v) = kv.strip_prefix("updateMask.fieldPaths=") {
                mask.push(v.to_string());
            }
        }
    }
    Some((resource, mask))
}

/// `name = projects/{p}/databases/(default)/documents/{path}` -> `{path}`.
fn doc_path_from_name(name: &str) -> String {
    name.split(DOCS_MARKER).nth(1).unwrap_or(name).trim_start_matches('/').to_string()
}

/// Render a Mongo doc as a Firestore REST document JSON object.
fn firestore_doc(path: &str, doc: &Document) -> Value {
    json!({
        "name": format!("projects/_/databases/(default)/documents/{}", path),
        "fields": document_to_fields(doc),
        "createTime": "1970-01-01T00:00:00Z",
        "updateTime": "1970-01-01T00:00:00Z",
    })
}

fn op_to_mongo(op: &str, value: Bson) -> Document {
    match op {
        "NOT_EQUAL" => doc! {"$ne": value},
        "LESS_THAN" => doc! {"$lt": value},
        "LESS_THAN_OR_EQUAL" => doc! {"$lte": value},
        "GREATER_THAN" => doc! {"$gt": value},
        "GREATER_THAN_OR_EQUAL" => doc! {"$gte": value},
        "ARRAY_CONTAINS" => doc! {"$eq": value},
        "IN" | "ARRAY_CONTAINS_ANY" => match value {
            Bson::Array(a) => doc! {"$in": a},
            other => doc! {"$in": [other]},
        },
        // EQUAL and anything unrecognized
        _ => doc! {"$eq": value},
    }
}

/// Translate a structuredQuery `where` (fieldFilter or compositeFilter) into a
/// Mongo match document.
fn where_to_filter(w: &Value) -> Document {
    if let Some(ff) = w.get("fieldFilter") {
        let field = ff.get("field").and_then(|f| f.get("fieldPath")).and_then(|v| v.as_str()).unwrap_or("");
        let op = ff.get("op").and_then(|v| v.as_str()).unwrap_or("EQUAL");
        let value = value_to_bson(ff.get("value").unwrap_or(&Value::Null));
        let mut d = Document::new();
        d.insert(field, op_to_mongo(op, value));
        d
    } else if let Some(comp) = w.get("compositeFilter") {
        let op = comp.get("op").and_then(|v| v.as_str()).unwrap_or("AND");
        let subs: Vec<Bson> = comp
            .get("filters")
            .and_then(|f| f.as_array())
            .map(|arr| arr.iter().map(|f| Bson::Document(where_to_filter(f))).collect())
            .unwrap_or_default();
        let key = if op.eq_ignore_ascii_case("OR") { "$or" } else { "$and" };
        let mut d = Document::new();
        d.insert(key, subs);
        d
    } else {
        Document::new()
    }
}

/// Dispatch a Firestore REST request against MongoDB. Returns (http_status, body).
pub async fn dispatch(mongo: &MongoStore, method: &str, url: &str, body: Option<&Value>) -> (u16, Value) {
    let (resource, mask) = match parse_firestore_url(url) {
        Some(x) => x,
        None => return (400, json!({"error": "unrecognized firestore url"})),
    };

    if resource == ":commit" || resource.ends_with(":commit") {
        return commit(mongo, body).await;
    }
    if let Some(parent) = resource.strip_suffix(":runQuery") {
        return run_query(mongo, parent, body).await;
    }

    match method {
        "GET" => {
            // Firestore: even segment count = document, odd = collection.
            if resource.split('/').count() % 2 == 0 {
                match mongo.get(&resource).await {
                    Ok(Some(d)) => (200, firestore_doc(&resource, &d)),
                    Ok(None) => (404, json!({"error": {"status": "NOT_FOUND"}})),
                    Err(e) => (500, json!({"error": e.to_string()})),
                }
            } else {
                match mongo.query(&resource, Document::new(), None, 0, None).await {
                    Ok(docs) => {
                        let documents: Vec<Value> = docs
                            .iter()
                            .map(|d| firestore_doc(d.get_str("_id").unwrap_or(""), d))
                            .collect();
                        (200, json!({ "documents": documents }))
                    }
                    Err(e) => (500, json!({"error": e.to_string()})),
                }
            }
        }
        "PATCH" => {
            let fields = body.and_then(|b| b.get("fields")).and_then(|f| f.as_object());
            let mut set_doc = Document::new();
            if let Some(f) = fields {
                for (k, v) in f {
                    set_doc.insert(k.clone(), value_to_bson(v));
                }
            }
            let mut update = Document::new();
            if !set_doc.is_empty() {
                update.insert("$set", set_doc.clone());
            }
            // updateMask entries absent from the body => field deletions.
            let mut unset = Document::new();
            for m in &mask {
                if fields.map(|f| !f.contains_key(m)).unwrap_or(true) {
                    unset.insert(m.clone(), "");
                }
            }
            if !unset.is_empty() {
                update.insert("$unset", unset);
            }
            if update.is_empty() {
                update.insert("$set", Document::new());
            }
            match mongo.apply(&resource, update, true).await {
                Ok(_) => match mongo.get(&resource).await {
                    Ok(Some(d)) => (200, firestore_doc(&resource, &d)),
                    _ => (200, firestore_doc(&resource, &set_doc)),
                },
                Err(e) => (500, json!({"error": e.to_string()})),
            }
        }
        "DELETE" => match mongo.delete(&resource).await {
            Ok(_) => (200, json!({})),
            Err(e) => (500, json!({"error": e.to_string()})),
        },
        _ => (405, json!({"error": "method not allowed"})),
    }
}

async fn run_query(mongo: &MongoStore, parent: &str, body: Option<&Value>) -> (u16, Value) {
    let sq = match body.and_then(|b| b.get("structuredQuery")) {
        Some(q) => q,
        None => return (400, json!({"error": "missing structuredQuery"})),
    };
    let collection_id = sq
        .get("from")
        .and_then(|f| f.as_array())
        .and_then(|a| a.first())
        .and_then(|f| f.get("collectionId"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let coll_path = if parent.is_empty() {
        collection_id.to_string()
    } else {
        format!("{}/{}", parent, collection_id)
    };

    let mut extra = Document::new();
    if let Some(w) = sq.get("where") {
        for (k, v) in where_to_filter(w) {
            extra.insert(k, v);
        }
    }
    let sort = sq.get("orderBy").and_then(|o| o.as_array()).and_then(|arr| {
        let mut s = Document::new();
        for ob in arr {
            let field = ob.get("field").and_then(|f| f.get("fieldPath")).and_then(|v| v.as_str()).unwrap_or("");
            let dir = ob.get("direction").and_then(|v| v.as_str()).unwrap_or("ASCENDING");
            s.insert(field, if dir == "DESCENDING" { -1 } else { 1 });
        }
        if s.is_empty() {
            None
        } else {
            Some(s)
        }
    });
    let limit = sq.get("limit").and_then(|v| v.as_i64());
    let offset = sq.get("offset").and_then(|v| v.as_u64()).unwrap_or(0);

    match mongo.query(&coll_path, extra, sort, offset, limit).await {
        Ok(docs) => {
            let rows: Vec<Value> = docs
                .iter()
                .map(|d| json!({ "document": firestore_doc(d.get_str("_id").unwrap_or(""), d) }))
                .collect();
            (200, Value::Array(rows))
        }
        Err(e) => (500, json!({"error": e.to_string()})),
    }
}

async fn commit(mongo: &MongoStore, body: Option<&Value>) -> (u16, Value) {
    let writes = match body.and_then(|b| b.get("writes")).and_then(|w| w.as_array()) {
        Some(w) => w,
        None => return (400, json!({"error": "missing writes"})),
    };
    let mut results = Vec::new();
    for w in writes {
        if let Some(update) = w.get("update") {
            let path = doc_path_from_name(update.get("name").and_then(|v| v.as_str()).unwrap_or(""));
            let mut set_doc = Document::new();
            if let Some(f) = update.get("fields").and_then(|f| f.as_object()) {
                for (k, v) in f {
                    set_doc.insert(k.clone(), value_to_bson(v));
                }
            }
            let _ = mongo.apply(&path, doc! {"$set": set_doc}, true).await;
        } else if let Some(transform) = w.get("transform") {
            let path = doc_path_from_name(transform.get("document").and_then(|v| v.as_str()).unwrap_or(""));
            let mut inc = Document::new();
            if let Some(fts) = transform.get("fieldTransforms").and_then(|v| v.as_array()) {
                for ft in fts {
                    let fp = ft.get("fieldPath").and_then(|v| v.as_str()).unwrap_or("");
                    if let Some(incv) = ft.get("increment") {
                        inc.insert(fp, value_to_bson(incv));
                    }
                }
            }
            if !inc.is_empty() {
                let _ = mongo.apply(&path, doc! {"$inc": inc}, true).await;
            }
        } else if let Some(del) = w.get("delete").and_then(|v| v.as_str()) {
            let _ = mongo.delete(&doc_path_from_name(del)).await;
        }
        results.push(json!({ "updateTime": "1970-01-01T00:00:00Z" }));
    }
    (200, json!({ "writeResults": results }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scalar_roundtrips() {
        assert_eq!(
            value_to_bson(&json!({"stringValue": "hi"})),
            Bson::String("hi".into())
        );
        assert_eq!(
            value_to_bson(&json!({"integerValue": "42"})),
            Bson::Int64(42)
        );
        assert_eq!(value_to_bson(&json!({"integerValue": 7})), Bson::Int64(7)); // tolerate raw number
        assert_eq!(
            value_to_bson(&json!({"doubleValue": 1.5})),
            Bson::Double(1.5)
        );
        assert_eq!(
            value_to_bson(&json!({"booleanValue": true})),
            Bson::Boolean(true)
        );
        assert_eq!(value_to_bson(&json!({"nullValue": null})), Bson::Null);
    }

    #[test]
    fn timestamp_to_bson_datetime() {
        let b = value_to_bson(&json!({"timestampValue": "2026-06-08T10:00:00Z"}));
        match b {
            Bson::DateTime(_) => {}
            other => panic!("expected datetime, got {:?}", other),
        }
        // round-trips back to an RFC3339 timestampValue
        let v = bson_to_value(&b);
        assert!(v
            .get("timestampValue")
            .and_then(|t| t.as_str())
            .unwrap()
            .starts_with("2026-06-08T10:00:00"));
    }

    #[test]
    fn nested_map_and_array() {
        let fs = json!({
            "mapValue": { "fields": {
                "active": {"booleanValue": true},
                "fingerprints": {"mapValue": {"fields": {"openai": {"stringValue": "abc"}}}},
                "tags": {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}},
            }}
        });
        let b = value_to_bson(&fs);
        let d = b.as_document().unwrap();
        assert_eq!(d.get_bool("active").unwrap(), true);
        assert_eq!(
            d.get_document("fingerprints")
                .unwrap()
                .get_str("openai")
                .unwrap(),
            "abc"
        );
        assert_eq!(d.get_array("tags").unwrap().len(), 2);
    }

    #[test]
    fn document_to_fields_skips_reserved() {
        let mut d = Document::new();
        d.insert("_id", "users/u/action_items/1");
        d.insert("_p", "users/u/action_items");
        d.insert("_k", "1");
        d.insert("description", "buy milk");
        d.insert("completed", false);
        let fields = document_to_fields(&d);
        let obj = fields.as_object().unwrap();
        assert!(!obj.contains_key("_id") && !obj.contains_key("_p") && !obj.contains_key("_k"));
        assert_eq!(obj["description"], json!({"stringValue": "buy milk"}));
        assert_eq!(obj["completed"], json!({"booleanValue": false}));
    }

    #[test]
    fn fields_to_document_builds_bson() {
        let fields = json!({
            "description": {"stringValue": "x"},
            "relevance_score": {"integerValue": "5"},
            "completed": {"booleanValue": true},
        });
        let d = fields_to_document(fields.as_object().unwrap());
        assert_eq!(d.get_str("description").unwrap(), "x");
        assert_eq!(d.get_i64("relevance_score").unwrap(), 5);
        assert_eq!(d.get_bool("completed").unwrap(), true);
    }

    #[test]
    fn full_value_roundtrip_preserves_shape() {
        let original = json!({
            "mapValue": {"fields": {
                "n": {"integerValue": "10"},
                "f": {"doubleValue": 2.5},
                "s": {"stringValue": "hi"},
                "b": {"booleanValue": false},
            }}
        });
        let back = bson_to_value(&value_to_bson(&original));
        // integerValue stays string-encoded per the REST contract
        let bf = back["mapValue"]["fields"].as_object().unwrap();
        assert_eq!(bf["n"], json!({"integerValue": "10"}));
        assert_eq!(bf["f"], json!({"doubleValue": 2.5}));
        assert_eq!(bf["s"], json!({"stringValue": "hi"}));
        assert_eq!(bf["b"], json!({"booleanValue": false}));
    }

    // --- dispatcher tests ---

    #[test]
    fn url_parsing_and_firestore_detection() {
        assert!(is_firestore_url("https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents/users/u"));
        assert!(!is_firestore_url("https://compute.googleapis.com/compute/v1/projects/p/zones"));
        let (r, m) = parse_firestore_url("https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents/users/u?updateMask.fieldPaths=agentVm").unwrap();
        assert_eq!(r, "users/u");
        assert_eq!(m, vec!["agentVm".to_string()]);
        let (r2, _) = parse_firestore_url("https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents:commit").unwrap();
        assert_eq!(r2, ":commit");
        let (r3, _) = parse_firestore_url("https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents/users/u:runQuery").unwrap();
        assert_eq!(r3, "users/u:runQuery");
    }

    #[test]
    fn where_filter_translation() {
        let ff = json!({"fieldFilter": {"field": {"fieldPath": "completed"}, "op": "EQUAL", "value": {"booleanValue": false}}});
        let d = where_to_filter(&ff);
        assert_eq!(d.get_document("completed").unwrap().get_bool("$eq").unwrap(), false);
        let comp = json!({"compositeFilter": {"op": "AND", "filters": [
            {"fieldFilter": {"field": {"fieldPath": "a"}, "op": "EQUAL", "value": {"stringValue": "x"}}},
            {"fieldFilter": {"field": {"fieldPath": "n"}, "op": "GREATER_THAN_OR_EQUAL", "value": {"integerValue": "5"}}}
        ]}});
        let d2 = where_to_filter(&comp);
        assert_eq!(d2.get_array("$and").unwrap().len(), 2);
    }

    fn furl(path: &str) -> String {
        format!("https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents/{}", path)
    }

    /// Drive the dispatcher with the exact REST shapes firestore.rs produces,
    /// against a real MongoDB. Run with
    /// `MONGO_TEST_URL=mongodb://localhost:27018 cargo test -- --ignored shim_dispatch_roundtrip`.
    #[tokio::test]
    #[ignore]
    async fn shim_dispatch_roundtrip() {
        let url = std::env::var("MONGO_TEST_URL").expect("set MONGO_TEST_URL");
        let mongo = MongoStore::connect(&url, "omi_shim_test").await.unwrap();
        let _ = mongo.delete("users/u/action_items/a1").await;

        // PATCH create (action_items create_action_item shape) -> GET back
        let body = json!({"fields": {
            "description": {"stringValue": "buy milk"},
            "completed": {"booleanValue": false},
            "created_at": {"timestampValue": "2026-06-08T10:00:00Z"},
        }});
        let (st, _) = dispatch(&mongo, "PATCH", &furl("users/u/action_items/a1"), Some(&body)).await;
        assert_eq!(st, 200);
        let (st, doc) = dispatch(&mongo, "GET", &furl("users/u/action_items/a1"), None).await;
        assert_eq!(st, 200);
        assert_eq!(doc["fields"]["description"]["stringValue"], "buy milk");
        assert_eq!(doc["fields"]["completed"]["booleanValue"], false);

        // :runQuery (get_action_items shape): where completed==false, order created_at desc
        let q = json!({"structuredQuery": {
            "from": [{"collectionId": "action_items"}],
            "where": {"fieldFilter": {"field": {"fieldPath": "completed"}, "op": "EQUAL", "value": {"booleanValue": false}}},
            "orderBy": [{"field": {"fieldPath": "created_at"}, "direction": "DESCENDING"}],
            "limit": 50
        }});
        let (st, rows) = dispatch(&mongo, "POST", &furl("users/u:runQuery"), Some(&q)).await;
        assert_eq!(st, 200);
        let arr = rows.as_array().unwrap();
        assert!(arr.iter().any(|r| r["document"]["fields"]["description"]["stringValue"] == "buy milk"));

        // :commit with field-transform increment (record_llm_usage shape)
        let _ = mongo.delete("users/u/llm_usage/2026-06-08").await;
        let commit = json!({"writes": [{"transform": {
            "document": "projects/p/databases/(default)/documents/users/u/llm_usage/2026-06-08",
            "fieldTransforms": [
                {"fieldPath": "desktop_chat.call_count", "increment": {"integerValue": "1"}},
                {"fieldPath": "desktop_chat.cost_usd", "increment": {"doubleValue": 0.5}}
            ]
        }}]});
        let curl = "https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents:commit";
        dispatch(&mongo, "POST", curl, Some(&commit)).await;
        dispatch(&mongo, "POST", curl, Some(&commit)).await;
        let usage = mongo.get("users/u/llm_usage/2026-06-08").await.unwrap().unwrap();
        let dc = usage.get_document("desktop_chat").unwrap();
        assert_eq!(dc.get_i64("call_count").unwrap(), 2);
        assert!((dc.get_f64("cost_usd").unwrap() - 1.0).abs() < 1e-9);

        // PATCH with updateMask (merge agentVm) then field deletion (unset)
        let setvm = json!({"fields": {"agentVm": {"mapValue": {"fields": {"vmName": {"stringValue": "vm-1"}}}}}});
        dispatch(&mongo, "PATCH", &furl("users/u?updateMask.fieldPaths=agentVm"), Some(&setvm)).await;
        let (_, ud) = dispatch(&mongo, "GET", &furl("users/u"), None).await;
        assert_eq!(ud["fields"]["agentVm"]["mapValue"]["fields"]["vmName"]["stringValue"], "vm-1");
        // unset: empty body + mask
        dispatch(&mongo, "PATCH", &furl("users/u?updateMask.fieldPaths=agentVm"), Some(&json!({"fields": {}}))).await;
        let raw = mongo.get("users/u").await.unwrap().unwrap();
        assert!(!raw.contains_key("agentVm"));

        // DELETE
        dispatch(&mongo, "DELETE", &furl("users/u/action_items/a1"), None).await;
        let (st, _) = dispatch(&mongo, "GET", &furl("users/u/action_items/a1"), None).await;
        assert_eq!(st, 404);

        // cleanup
        let _ = mongo.delete("users/u").await;
        let _ = mongo.delete("users/u/llm_usage/2026-06-08").await;
    }
}
