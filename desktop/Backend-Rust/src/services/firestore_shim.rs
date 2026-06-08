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
}
