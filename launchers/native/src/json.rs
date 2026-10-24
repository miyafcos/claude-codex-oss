//! JSON objects with PowerShell's case-insensitive property access. Reject
//! duplicates before serde_json can silently discard them (including exact
//! duplicates and case-only duplicates at any nesting depth).
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};
use std::fmt;
struct Checked(Value);
impl<'de> Deserialize<'de> for Checked {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Checked;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("unambiguous JSON")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Checked, E> {
                Ok(Checked(Value::Bool(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Checked, E> {
                Ok(Checked(Value::Number(v.into())))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Checked, E> {
                Ok(Checked(Value::Number(v.into())))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Checked, E> {
                Number::from_f64(v)
                    .map(|n| Checked(Value::Number(n)))
                    .ok_or_else(|| E::custom("number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Checked, E> {
                if v.contains('\0') {
                    return Err(E::custom("NUL string"));
                }
                Ok(Checked(Value::String(v.into())))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Checked, E> {
                if v.contains('\0') {
                    return Err(E::custom("NUL string"));
                }
                Ok(Checked(Value::String(v)))
            }
            fn visit_none<E: de::Error>(self) -> Result<Checked, E> {
                Ok(Checked(Value::Null))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Checked, E> {
                Ok(Checked(Value::Null))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Checked, A::Error> {
                let mut values = Vec::new();
                while let Some(Checked(v)) = a.next_element()? {
                    values.push(v);
                }
                Ok(Checked(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Checked, A::Error> {
                let mut values = Map::new();
                while let Some((key, Checked(value))) = a.next_entry::<String, Checked>()? {
                    if !key.is_ascii() || key.is_empty() || key.contains('\0') {
                        return Err(de::Error::custom("ambiguous property name"));
                    }
                    let key = key.to_ascii_lowercase();
                    if [
                        "psobject",
                        "psbase",
                        "psadapted",
                        "psextended",
                        "pstypenames",
                    ]
                    .contains(&key.as_str())
                    {
                        return Err(de::Error::custom("PowerShell reserved property"));
                    }
                    if values.insert(key, value).is_some() {
                        return Err(de::Error::custom("duplicate property"));
                    }
                }
                Ok(Checked(Value::Object(values)))
            }
        }
        d.deserialize_any(V)
    }
}
pub fn parse(s: &str) -> Result<Value, serde_json::Error> {
    serde_json::from_str::<Checked>(s.trim_start_matches('\u{feff}')).map(|v| v.0)
}
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::Array(a) => match a.len() {
            0 => false,
            1 => truthy(&a[0]),
            _ => true,
        },
        Value::Object(_) => true,
    }
}
pub fn positive_int(s: &str) -> Option<i32> {
    s.trim_matches(|c| matches!(c, '\t' | '\n' | '\u{000b}' | '\u{000c}' | '\r' | ' '))
        .parse::<i32>()
        .ok()
        .filter(|x| *x > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duplicate_keys_fail_closed() {
        for s in [
            r#"{"a":1,"A":2}"#,
            r#"{"a":1,"a":2}"#,
            r#"{"x":{"A":1,"a":2}}"#,
        ] {
            assert!(parse(s).is_err());
        }
    }
    #[test]
    fn casefold() {
        assert_eq!(
            parse(r#"{"Aliases":{"OPUS":"x"}}"#).unwrap()["aliases"]["opus"],
            "x"
        );
    }
    #[test]
    fn int32() {
        assert_eq!(positive_int(" +2147483647 "), Some(i32::MAX));
        for s in ["0", "-1", "2147483648", "1.0", "1e3", "", "\u{00a0}12"] {
            assert_eq!(positive_int(s), None);
        }
    }
    #[test]
    fn powershell_truth() {
        for s in ["null", "false", "0", "[]", r#"[""]"#] {
            assert!(!truthy(&parse(s).unwrap()));
        }
        assert!(truthy(&parse(r#"["",""]"#).unwrap()));
    }
}
