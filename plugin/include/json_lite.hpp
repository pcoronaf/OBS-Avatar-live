/*
OBS Avatar Live
Copyright (C) 2026 OBS Avatar Live contributors

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program. If not, see <https://www.gnu.org/licenses/>
*/

// Minimal JSON value / parser / serializer for the control protocol.
// Intentionally small: no libobs dependency so the IPC layer can be built and
// tested standalone (tests/native).

#pragma once

#include <cstdint>
#include <cstdio>
#include <charconv>
#include <cmath>
#include <string>
#include <utility>
#include <vector>

namespace json {

class Value {
public:
	enum class Type { Null, Bool, Number, String, Array, Object };

	Value() = default;
	Value(std::nullptr_t) {}
	Value(bool b) : type_(Type::Bool), bool_(b) {}
	Value(int v) : type_(Type::Number), num_(static_cast<double>(v)) {}
	Value(unsigned v) : type_(Type::Number), num_(static_cast<double>(v)) {}
	Value(long v) : type_(Type::Number), num_(static_cast<double>(v)) {}
	Value(unsigned long v) : type_(Type::Number), num_(static_cast<double>(v)) {}
	Value(long long v) : type_(Type::Number), num_(static_cast<double>(v)) {}
	Value(unsigned long long v) : type_(Type::Number), num_(static_cast<double>(v)) {}
	Value(double v) : type_(Type::Number), num_(v) {}
	Value(const char *s) : type_(Type::String), str_(s ? s : "") {}
	Value(std::string s) : type_(Type::String), str_(std::move(s)) {}

	static Value object()
	{
		Value v;
		v.type_ = Type::Object;
		return v;
	}
	static Value array()
	{
		Value v;
		v.type_ = Type::Array;
		return v;
	}

	Type type() const { return type_; }
	bool is_null() const { return type_ == Type::Null; }
	bool is_bool() const { return type_ == Type::Bool; }
	bool is_number() const { return type_ == Type::Number; }
	bool is_string() const { return type_ == Type::String; }
	bool is_array() const { return type_ == Type::Array; }
	bool is_object() const { return type_ == Type::Object; }

	bool as_bool(bool def = false) const { return type_ == Type::Bool ? bool_ : def; }
	double as_number(double def = 0.0) const { return type_ == Type::Number ? num_ : def; }
	int64_t as_int(int64_t def = 0) const { return type_ == Type::Number ? static_cast<int64_t>(num_) : def; }
	uint64_t as_uint(uint64_t def = 0) const
	{
		return (type_ == Type::Number && num_ >= 0) ? static_cast<uint64_t>(num_) : def;
	}
	const std::string &as_string() const { return str_; }
	std::string as_string(const std::string &def) const { return type_ == Type::String ? str_ : def; }

	// Array access
	size_t size() const
	{
		return type_ == Type::Array ? items_.size() : (type_ == Type::Object ? keys_.size() : 0);
	}
	const Value &at(size_t i) const { return (type_ == Type::Array && i < items_.size()) ? items_[i] : null_ref(); }
	void push_back(Value v)
	{
		if (type_ != Type::Array) {
			*this = array();
		}
		items_.push_back(std::move(v));
	}

	// Object access
	bool has(const std::string &key) const { return find(key) != nullptr; }
	const Value &operator[](const std::string &key) const
	{
		const Value *v = find(key);
		return v ? *v : null_ref();
	}
	Value &set(const std::string &key, Value v)
	{
		if (type_ != Type::Object) {
			*this = object();
		}
		for (size_t i = 0; i < keys_.size(); i++) {
			if (keys_[i] == key) {
				items_[i] = std::move(v);
				return items_[i];
			}
		}
		keys_.push_back(key);
		items_.push_back(std::move(v));
		return items_.back();
	}
	const std::vector<std::string> &keys() const { return keys_; }
	const Value &value_at(size_t i) const { return i < items_.size() ? items_[i] : null_ref(); }

	std::string dump() const
	{
		std::string out;
		dump_to(out);
		return out;
	}

	void dump_to(std::string &out) const
	{
		switch (type_) {
		case Type::Null:
			out += "null";
			break;
		case Type::Bool:
			out += bool_ ? "true" : "false";
			break;
		case Type::Number:
			dump_number(out, num_);
			break;
		case Type::String:
			dump_string(out, str_);
			break;
		case Type::Array:
			out += '[';
			for (size_t i = 0; i < items_.size(); i++) {
				if (i)
					out += ',';
				items_[i].dump_to(out);
			}
			out += ']';
			break;
		case Type::Object:
			out += '{';
			for (size_t i = 0; i < keys_.size(); i++) {
				if (i)
					out += ',';
				dump_string(out, keys_[i]);
				out += ':';
				items_[i].dump_to(out);
			}
			out += '}';
			break;
		}
	}

	// Returns false (and sets *err) on malformed input. Max nesting depth 64.
	static bool parse(const std::string &text, Value &out, std::string *err = nullptr)
	{
		Parser p{text.data(), text.data() + text.size(), err};
		p.skip_ws();
		if (!p.parse_value(out, 0))
			return false;
		p.skip_ws();
		if (p.cur != p.end)
			return p.fail("trailing characters");
		return true;
	}

private:
	static const Value &null_ref()
	{
		static const Value n;
		return n;
	}

	const Value *find(const std::string &key) const
	{
		if (type_ != Type::Object)
			return nullptr;
		for (size_t i = 0; i < keys_.size(); i++)
			if (keys_[i] == key)
				return &items_[i];
		return nullptr;
	}

	static void dump_number(std::string &out, double d)
	{
		char buf[40];
		if (!std::isfinite(d)) {
			out += "null";
			return;
		}
		if (std::floor(d) == d && std::fabs(d) < 9007199254740992.0) {
			std::snprintf(buf, sizeof(buf), "%lld", static_cast<long long>(d));
		} else {
			auto res = std::to_chars(buf, buf + sizeof(buf), d); // locale independent
			out.append(buf, res.ptr);
			return;
		}
		out += buf;
	}

	static void dump_string(std::string &out, const std::string &s)
	{
		out += '"';
		for (unsigned char c : s) {
			switch (c) {
			case '"':
				out += "\\\"";
				break;
			case '\\':
				out += "\\\\";
				break;
			case '\b':
				out += "\\b";
				break;
			case '\f':
				out += "\\f";
				break;
			case '\n':
				out += "\\n";
				break;
			case '\r':
				out += "\\r";
				break;
			case '\t':
				out += "\\t";
				break;
			default:
				if (c < 0x20) {
					char buf[8];
					std::snprintf(buf, sizeof(buf), "\\u%04x", c);
					out += buf;
				} else {
					out += static_cast<char>(c);
				}
			}
		}
		out += '"';
	}

	struct Parser {
		const char *cur;
		const char *end;
		std::string *err;

		bool fail(const char *msg)
		{
			if (err)
				*err = msg;
			return false;
		}
		void skip_ws()
		{
			while (cur < end && (*cur == ' ' || *cur == '\t' || *cur == '\n' || *cur == '\r'))
				cur++;
		}
		bool literal(const char *lit)
		{
			const char *p = cur;
			for (; *lit; lit++, p++)
				if (p >= end || *p != *lit)
					return false;
			cur = p;
			return true;
		}
		bool parse_value(Value &out, int depth)
		{
			if (depth > 64)
				return fail("nesting too deep");
			if (cur >= end)
				return fail("unexpected end");
			char c = *cur;
			if (c == '{')
				return parse_object(out, depth);
			if (c == '[')
				return parse_array(out, depth);
			if (c == '"') {
				std::string s;
				if (!parse_string(s))
					return false;
				out = Value(std::move(s));
				return true;
			}
			if (c == 't') {
				if (literal("true")) {
					out = Value(true);
					return true;
				}
				return fail("bad literal");
			}
			if (c == 'f') {
				if (literal("false")) {
					out = Value(false);
					return true;
				}
				return fail("bad literal");
			}
			if (c == 'n') {
				if (literal("null")) {
					out = Value();
					return true;
				}
				return fail("bad literal");
			}
			return parse_number(out);
		}
		bool parse_number(Value &out)
		{
			const char *start = cur;
			if (cur < end && *cur == '-')
				cur++;
			bool digits = false;
			while (cur < end && *cur >= '0' && *cur <= '9') {
				cur++;
				digits = true;
			}
			if (cur < end && *cur == '.') {
				cur++;
				while (cur < end && *cur >= '0' && *cur <= '9') {
					cur++;
					digits = true;
				}
			}
			if (cur < end && (*cur == 'e' || *cur == 'E')) {
				cur++;
				if (cur < end && (*cur == '+' || *cur == '-'))
					cur++;
				while (cur < end && *cur >= '0' && *cur <= '9')
					cur++;
			}
			if (!digits)
				return fail("bad number");
			// from_chars is locale independent (strtod is not: es-ES uses ',').
			double d = 0.0;
			auto res = std::from_chars(start, cur, d);
			if (res.ec != std::errc() || res.ptr != cur)
				return fail("bad number");
			out = Value(d);
			return true;
		}
		static void append_utf8(std::string &s, uint32_t cp)
		{
			if (cp < 0x80) {
				s += static_cast<char>(cp);
			} else if (cp < 0x800) {
				s += static_cast<char>(0xC0 | (cp >> 6));
				s += static_cast<char>(0x80 | (cp & 0x3F));
			} else if (cp < 0x10000) {
				s += static_cast<char>(0xE0 | (cp >> 12));
				s += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
				s += static_cast<char>(0x80 | (cp & 0x3F));
			} else {
				s += static_cast<char>(0xF0 | (cp >> 18));
				s += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
				s += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
				s += static_cast<char>(0x80 | (cp & 0x3F));
			}
		}
		bool hex4(uint32_t &v)
		{
			if (end - cur < 4)
				return fail("bad \\u escape");
			v = 0;
			for (int i = 0; i < 4; i++) {
				char h = *cur++;
				v <<= 4;
				if (h >= '0' && h <= '9')
					v |= static_cast<uint32_t>(h - '0');
				else if (h >= 'a' && h <= 'f')
					v |= static_cast<uint32_t>(h - 'a' + 10);
				else if (h >= 'A' && h <= 'F')
					v |= static_cast<uint32_t>(h - 'A' + 10);
				else
					return fail("bad \\u escape");
			}
			return true;
		}
		bool parse_string(std::string &s)
		{
			cur++; // opening quote
			while (cur < end) {
				char c = *cur++;
				if (c == '"')
					return true;
				if (static_cast<unsigned char>(c) < 0x20)
					return fail("control char in string");
				if (c != '\\') {
					s += c;
					continue;
				}
				if (cur >= end)
					break;
				char e = *cur++;
				switch (e) {
				case '"':
					s += '"';
					break;
				case '\\':
					s += '\\';
					break;
				case '/':
					s += '/';
					break;
				case 'b':
					s += '\b';
					break;
				case 'f':
					s += '\f';
					break;
				case 'n':
					s += '\n';
					break;
				case 'r':
					s += '\r';
					break;
				case 't':
					s += '\t';
					break;
				case 'u': {
					uint32_t cp;
					if (!hex4(cp))
						return false;
					if (cp >= 0xD800 && cp <= 0xDBFF) {
						uint32_t lo;
						if (end - cur < 6 || cur[0] != '\\' || cur[1] != 'u')
							return fail("bad surrogate");
						cur += 2;
						if (!hex4(lo) || lo < 0xDC00 || lo > 0xDFFF)
							return fail("bad surrogate");
						cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
					} else if (cp >= 0xDC00 && cp <= 0xDFFF) {
						return fail("bad surrogate");
					}
					append_utf8(s, cp);
					break;
				}
				default:
					return fail("bad escape");
				}
			}
			return fail("unterminated string");
		}
		bool parse_array(Value &out, int depth)
		{
			cur++;
			out = Value::array();
			skip_ws();
			if (cur < end && *cur == ']') {
				cur++;
				return true;
			}
			for (;;) {
				Value item;
				skip_ws();
				if (!parse_value(item, depth + 1))
					return false;
				out.push_back(std::move(item));
				skip_ws();
				if (cur >= end)
					return fail("unterminated array");
				if (*cur == ',') {
					cur++;
					continue;
				}
				if (*cur == ']') {
					cur++;
					return true;
				}
				return fail("expected , or ]");
			}
		}
		bool parse_object(Value &out, int depth)
		{
			cur++;
			out = Value::object();
			skip_ws();
			if (cur < end && *cur == '}') {
				cur++;
				return true;
			}
			for (;;) {
				skip_ws();
				if (cur >= end || *cur != '"')
					return fail("expected key");
				std::string key;
				if (!parse_string(key))
					return false;
				skip_ws();
				if (cur >= end || *cur != ':')
					return fail("expected :");
				cur++;
				skip_ws();
				Value v;
				if (!parse_value(v, depth + 1))
					return false;
				out.set(key, std::move(v));
				skip_ws();
				if (cur >= end)
					return fail("unterminated object");
				if (*cur == ',') {
					cur++;
					continue;
				}
				if (*cur == '}') {
					cur++;
					return true;
				}
				return fail("expected , or }");
			}
		}
	};

	Type type_ = Type::Null;
	bool bool_ = false;
	double num_ = 0.0;
	std::string str_;
	std::vector<std::string> keys_;
	std::vector<Value> items_; // array items, or object values (parallel to keys_)
};

} // namespace json
