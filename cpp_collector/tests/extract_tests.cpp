#include "../src/extract.hpp"
#include <iostream>
#include <stdexcept>
void check(bool b){if(!b)throw std::runtime_error("parser test failed");}
int main(){
    constexpr size_t chunk=1<<20;
    const std::string response=R"({"id":"resp_cross_boundary","object":"response","model":"actual","completed_at":100})";
    std::string memory(chunk*2,'P');memory.replace(chunk-30,response.size(),response);
    capture::Batch without,with;
    for(size_t off=0;off<memory.size();off+=chunk){
        capture::extract(std::string_view(memory).substr(off,std::min(chunk,memory.size()-off)),without,chunk);
        capture::extract(std::string_view(memory).substr(off,std::min(chunk+capture::window_size,memory.size()-off)),with,chunk);
    }
    check(without.responses.empty());check(with.responses.size()==1);
    check(with.responses[0].at("response_id")=="resp_cross_boundary");
    std::string req=R"({"type":"response.create","input":[{"content":")"+std::string(16000,'x')+R"("}],"model":"actual","previous_response_id":"resp_parent"})";
    capture::Batch long_request;capture::extract(req,long_request);
    check(long_request.requests.size()==1);check(long_request.requests[0].at("prev")=="resp_parent");
    std::cout<<"PASS: unique boundary object requires overlap; late request fields survive long input.\n";
}
