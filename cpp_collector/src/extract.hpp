#pragma once
#include <algorithm>
#include <cctype>
#include <map>
#include <string>
#include <string_view>
#include <vector>

namespace capture {
constexpr size_t window_size = 256 * 1024;
using Fields = std::map<std::string, std::string>;
struct Batch { std::vector<Fields> responses, requests; };
inline void ws(std::string_view s, size_t& p) {
    while (p < s.size() && (s[p]==' ' || s[p]=='\n' || s[p]=='\r' || s[p]=='\t')) ++p;
}
inline bool string_end(std::string_view s, size_t& p) {
    if (p >= s.size() || s[p++] != '"') return false;
    while (p < s.size()) {
        unsigned char c = s[p++];
        if (c == '"') return true;
        if (c < 32) return false;
        if (c == '\\') {
            if (p >= s.size()) return false;
            if (s[p++] == 'u') {
                for (int i=0; i<4; ++i) if (p>=s.size() || !std::isxdigit(static_cast<unsigned char>(s[p++]))) return false;
            }
        }
    }
    return false;
}
inline bool skip(std::string_view s, size_t& p, unsigned depth=0) {
    ws(s,p);
    if (p>=s.size() || depth>64) return false;
    if (s[p]=='"') return string_end(s,p);
    if (s[p]=='{' || s[p]=='[') {
        char close = s[p++]=='{' ? '}' : ']';
        // Balanced containers, respecting strings. Only direct object fields are captured.
        while (p<s.size()) {
            if (s[p]==close) { ++p; return true; }
            if (s[p]=='"' || s[p]=='{' || s[p]=='[') { if (!skip(s,p,depth+1)) return false; }
            else { if (s[p]=='\0' || s[p]=='}' || s[p]==']') return false; ++p; }
        }
        return false;
    }
    const auto start=p;
    while (p<s.size() && s[p]!=',' && s[p]!='}' && s[p]!=']' && !std::isspace(static_cast<unsigned char>(s[p]))) {
        if (s[p]=='\0') return false;
        ++p;
    }
    return p>start;
}
inline std::string scalar(std::string_view v) {
    if (v.size()>256 || v.empty() || v=="null") return {};
    if (v.front()=='"' && v.back()=='"') {
        v.remove_prefix(1); v.remove_suffix(1);
        // Identifiers and model names should be plain UTF-8. Reject escaped ambiguous metadata.
        if (v.find('\\')!=v.npos) return {};
        return std::string(v);
    }
    if (std::all_of(v.begin(),v.end(),[](unsigned char c){return std::isdigit(c);})) return std::string(v);
    return {};
}
inline std::map<std::string,std::string_view> fields(std::string_view s) {
    std::map<std::string,std::string_view> out;
    size_t p=0; ws(s,p);
    if(p>=s.size() || s[p++]!='{') return out;
    for (size_t count=0;count<128;++count) {
        ws(s,p); if(p>=s.size() || s[p]=='}') break;
        size_t k=p; if(!string_end(s,p)) break;
        auto key=scalar(s.substr(k,p-k));
        ws(s,p); if(p>=s.size() || s[p++]!=':') break;
        ws(s,p); size_t start=p;
        if(!skip(s,p)) break;
        if(!key.empty()) {
            if(out.count(key)) return {}; // Duplicate direct fields are ambiguous.
            out.emplace(std::move(key),s.substr(start,p-start));
        }
        ws(s,p); if(p>=s.size() || s[p++]!=',') break;
    }
    return out;
}
inline std::string value(const std::map<std::string,std::string_view>& f, const char* key) {
    auto it=f.find(key); return it==f.end()?std::string():scalar(it->second);
}
inline bool rid(const std::string& s) {
    return s.size()>5 && s.rfind("resp_",0)==0 && std::all_of(s.begin()+5,s.end(),[](unsigned char c){return std::isalnum(c)||c=='_'||c=='-';});
}
inline void extract(std::string_view data, Batch& out, size_t start_limit=SIZE_MAX) {
    if(data.find("\"model\"")==data.npos || (data.find("resp_")==data.npos && data.find("response.create")==data.npos)) return;
    for(size_t p=data.find('{');p!=data.npos && p<start_limit;p=data.find('{',p+1)) {
        if(out.responses.size()+out.requests.size()>=10000) break;
        auto s=data.substr(p,std::min(window_size,data.size()-p));
        size_t k=1; ws(s,k); size_t begin=k; if(!string_end(s,k)) continue;
        const auto first=scalar(s.substr(begin,k-begin));
        if(first!="id" && first!="type" && first!="model" && first!="previous_response_id" && first!="object" && first!="store" && first!="stream" && first!="client_metadata") continue;
        auto f=fields(s); const auto model=value(f,"model");
        if(model.empty()) continue;
        const auto type=value(f,"type");
        if(type=="response.create") {
            Fields r{{"model",model}};
            const auto prev=value(f,"previous_response_id");
            if(rid(prev)) r["prev"]=prev;
            // A first request is observed but not paired by timing alone.
            r["source"]="memory_websocket";
            out.requests.push_back(std::move(r)); continue;
        }
        const auto id=value(f,"id"); if(!rid(id) || f.count("client_metadata")) continue;
        int marks=0;
        for(const char* key:{"safety_identifier","frequency_penalty","presence_penalty","completed_at","max_output_tokens"}) marks+=f.count(key)?1:0;
        if(value(f,"object")=="response") ++marks;
        if(marks<2) continue;
        Fields r{{"response_id",id},{"model",model},{"_marks",std::to_string(marks)}};
        for(const char* key:{"status","created_at","completed_at"}) {auto v=value(f,key); if(!v.empty())r[key]=v;}
        auto prev=value(f,"previous_response_id");if(rid(prev))r["prev"]=prev;
        if(auto it=f.find("reasoning");it!=f.end()) {auto effort=value(fields(it->second),"effort");if(!effort.empty())r["effort"]=effort;}
        if(auto it=f.find("text");it!=f.end()) {
            auto text=fields(it->second);if(auto fmt=text.find("format");fmt!=text.end())r["text_format"]=value(fields(fmt->second),"type");
        } else if(auto fmt=f.find("format");fmt!=f.end())r["text_format"]=value(fields(fmt->second),"type");
        out.responses.push_back(std::move(r));
    }
}
inline std::string quote(std::string_view s) {
    std::string o="\"";
    constexpr char hex[]="0123456789abcdef";
    for(unsigned char c:s) {
        if(c=='"'||c=='\\'){o+='\\';o+=static_cast<char>(c);}
        else if(c<32){o+="\\u00";o+=hex[c>>4];o+=hex[c&15];}
        else o+=static_cast<char>(c);
    }
    o+='"';return o;
}
inline std::string json(const Fields& f) {
    std::string o="{";for(const auto& [k,v]:f){if(o.size()>1)o+=',';o+=quote(k)+":"+quote(v);}return o+"}";
}
inline std::string array(const std::vector<Fields>& rows) {
    std::string o="[";for(const auto& row:rows){if(o.size()>1)o+=',';o+=json(row);}return o+"]";
}
}
