#pragma once
#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <map>
#include <string>
#include <string_view>
#include <vector>

namespace capture {
constexpr size_t window_size = 256 * 1024;
// Large input/tool arrays can precede model. Grow only candidate reads, not every block.
constexpr size_t max_window_size = 4 * 1024 * 1024;
using Fields = std::map<std::string, std::string>;
struct Batch {
    std::vector<Fields> responses, requests;
    uint64_t candidates=0, truncated=0;
    std::vector<size_t> retry_offsets;
};
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
enum FieldKey : size_t {
    kId, kType, kModel, kPrevious, kObject, kClientMetadata,
    kSafetyIdentifier, kFrequencyPenalty, kPresencePenalty, kCompletedAt,
    kMaxOutputTokens, kStatus, kCreatedAt, kReasoning, kText, kFormat, kInput,
    kEffort, kThreadId, kSessionId, kTurnId, kRootTurnId, kFieldCount
};
inline int field_key(std::string_view key) {
    switch(key.size()) {
    case 2:  if(key=="id")return kId; break;
    case 4:  if(key=="type")return kType;if(key=="text")return kText;break;
    case 5:  if(key=="model")return kModel;if(key=="input")return kInput;break;
    case 6:  if(key=="object")return kObject;if(key=="status")return kStatus;
             if(key=="format")return kFormat;if(key=="effort")return kEffort;break;
    case 7:  if(key=="turn_id")return kTurnId;break;
    case 9:  if(key=="reasoning")return kReasoning;if(key=="thread_id")return kThreadId;break;
    case 10: if(key=="created_at")return kCreatedAt;if(key=="session_id")return kSessionId;break;
    case 12: if(key=="completed_at")return kCompletedAt;if(key=="root_turn_id")return kRootTurnId;break;
    case 15: if(key=="client_metadata")return kClientMetadata;break;
    case 16: if(key=="presence_penalty")return kPresencePenalty;break;
    case 17: if(key=="safety_identifier")return kSafetyIdentifier;
             if(key=="frequency_penalty")return kFrequencyPenalty;
             if(key=="max_output_tokens")return kMaxOutputTokens;break;
    case 20: if(key=="previous_response_id")return kPrevious;break;
    }
    return -1;
}
// Only consumed fields receive slots. This avoids heap allocation and repeated linear
// searches while preserving duplicate detection for every field that affects evidence.
struct FieldViews {
    std::array<std::string_view,kFieldCount> values{};
    uint32_t present=0;
    bool add(int key,std::string_view value) {
        if(key<0)return true;
        const auto bit=uint32_t{1}<<key;
        if(present&bit)return false;
        present|=bit;values[static_cast<size_t>(key)]=value;return true;
    }
    bool count(FieldKey key) const {return (present&(uint32_t{1}<<key))!=0;}
    std::string_view get(FieldKey key) const {return count(key)?values[key]:std::string_view{};}
};
inline FieldViews fields(std::string_view s, size_t* complete=nullptr) {
    FieldViews out;
    size_t p=0; ws(s,p);
    if(p>=s.size() || s[p++]!='{') return out;
    for (size_t count=0;count<128;++count) {
        ws(s,p); if(p>=s.size()) break;
        if(s[p]=='}') { if(complete)*complete=p+1; break; }
        size_t k=p; if(!string_end(s,p)) break;
        auto key=s.substr(k+1,p-k-2);
        if(key.find('\\')!=key.npos)return {};
        ws(s,p); if(p>=s.size() || s[p++]!=':') break;
        ws(s,p); size_t start=p;
        if(!skip(s,p)) break;
        if(!out.add(field_key(key),s.substr(start,p-start)))return {}; // Relevant duplicate fields are ambiguous.
        ws(s,p); if(p>=s.size()) break;
        if(s[p]=='}') { if(complete)*complete=p+1; break; }
        if(s[p++]!=',') break;
    }
    return out;
}
inline std::string value(const FieldViews& f, FieldKey key) {
    return scalar(f.get(key));
}
inline bool rid(const std::string& s) {
    return s.size()>5 && s.rfind("resp_",0)==0 && std::all_of(s.begin()+5,s.end(),[](unsigned char c){return std::isalnum(c)||c=='_'||c=='-';});
}
inline bool candidate_block(std::string_view data) {
    // Most memory has neither marker. Avoid three/four full-buffer searches, especially
    // the unquoted response.create search whose first byte is common in ordinary text.
    return data.find("\"model\"")!=data.npos || data.find("\"response.create\"")!=data.npos;
}
inline void extract(std::string_view data,Batch& out,size_t start_limit=SIZE_MAX) {
    for(size_t p=data.find('{');p!=data.npos&&p<start_limit;p=data.find('{',p+1)) {
        if(out.responses.size()+out.requests.size()>=10000)break;
        auto s=data.substr(p,std::min(max_window_size,data.size()-p));
        size_t k=1;ws(s,k);const auto key_start=k;if(!string_end(s,k))continue;
        const auto first=s.substr(key_start+1,k-key_start-2);
        if(first=="type"||first=="id") {
            ws(s,k);if(k>=s.size()||s[k++]!=':')continue;
            ws(s,k);const auto start=k;if(!string_end(s,k))continue;
            const auto v=s.substr(start+1,k-start-2);
            if(first=="type"&&v!="response.create")continue;
            if(first=="id"&&v.substr(0,5)!="resp_")continue;
        }
        ++out.candidates;size_t complete=0;auto f=fields(s,&complete);const auto model=value(f,kModel);
        if(!complete) {
            ++out.truncated;
            if(s.size()<max_window_size&&(value(f,kType)=="response.create"||f.count(kClientMetadata)))out.retry_offsets.push_back(p);
            continue;
        }
        if(model.empty())continue;
        const auto type=value(f,kType);
        const bool http_candidate=type.empty()&&f.count(kInput)&&f.count(kClientMetadata)&&!f.count(kId)&&!f.count(kObject);
        if(type=="response.create"||http_candidate) {
            Fields r{{"model",model}};const auto prev=value(f,kPrevious);if(rid(prev))r["prev"]=prev;
            r["source"]=http_candidate?"memory_http_candidate":"memory_websocket";if(http_candidate)r["candidate_only"]="1";
            if(f.count(kClientMetadata)) {
                auto context=fields(f.get(kClientMetadata));
                for(const auto& [name,key]:std::initializer_list<std::pair<const char*,FieldKey>>{
                        {"thread_id",kThreadId},{"session_id",kSessionId},{"turn_id",kTurnId},{"root_turn_id",kRootTurnId}}) {
                    auto v=value(context,key);if(!v.empty())r[name]=v;
                }
            }
            out.requests.push_back(std::move(r));p+=complete-1;continue;
        }
        const auto id=value(f,kId);if(!rid(id)||f.count(kClientMetadata))continue;
        int marks=0;for(auto key:{kSafetyIdentifier,kFrequencyPenalty,kPresencePenalty,kCompletedAt,kMaxOutputTokens})marks+=f.count(key)?1:0;
        if(value(f,kObject)=="response")++marks;if(marks<2)continue;
        Fields r{{"response_id",id},{"model",model},{"_marks",std::to_string(marks)}};
        for(const auto& [name,key]:std::initializer_list<std::pair<const char*,FieldKey>>{
                {"status",kStatus},{"created_at",kCreatedAt},{"completed_at",kCompletedAt}}) {
            auto v=value(f,key);if(!v.empty())r[name]=v;
        }
        auto prev=value(f,kPrevious);if(rid(prev))r["prev"]=prev;
        if(f.count(kReasoning)){auto effort=value(fields(f.get(kReasoning)),kEffort);if(!effort.empty())r["effort"]=effort;}
        if(f.count(kText)){auto text=fields(f.get(kText));if(text.count(kFormat))r["text_format"]=value(fields(text.get(kFormat)),kType);}
        else if(f.count(kFormat))r["text_format"]=value(fields(f.get(kFormat)),kType);
        out.responses.push_back(std::move(r));p+=complete-1;
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
