#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <fstream>
#include <fcntl.h>
#include <io.h>
#include <iostream>
#include <mutex>
#include <set>
#include <thread>
#include "extract.hpp"
using Clock=std::chrono::steady_clock;
static double seconds(Clock::time_point t){return std::chrono::duration<double>(Clock::now()-t).count();}
struct Task{uintptr_t address;size_t size,owned;};
struct Result{capture::Batch batch;uint64_t bytes=0,hits=0,errors=0;double read=0,parse=0;std::set<uintptr_t> hot;};
class Scanner {
    HANDLE process;
    std::vector<std::thread> pool;
    std::mutex mutex;
    std::condition_variable begin,end;
    bool stopping=false;
    unsigned generation=0,done=0;
    std::atomic<size_t> next{0};
    std::vector<Task> tasks;
    std::vector<Result> results;
    std::set<uintptr_t> hot;
    std::vector<Task> cached;
    Clock::time_point enumerated{};
    size_t regions=0;
    double region_cost=0;
    static constexpr size_t chunk=1<<20;
    void worker(size_t index) {
        std::vector<char> buffer(chunk+capture::window_size);
        unsigned observed=0;
        for(;;){
            {std::unique_lock<std::mutex> lock(mutex);begin.wait(lock,[&]{return stopping||generation!=observed;});if(stopping)return;observed=generation;}
            Result r;
            for(size_t t=next.fetch_add(1);t<tasks.size();t=next.fetch_add(1)) {
                const auto task=tasks[t];SIZE_T got=0;
                auto t0=Clock::now();
                BOOL ok=ReadProcessMemory(process,reinterpret_cast<void*>(task.address),buffer.data(),task.size,&got);
                r.read+=seconds(t0);r.bytes+=got;if(!ok)++r.errors;
                if(got){
                    auto data=std::string_view(buffer.data(),got);
                    if(data.find("\"model\"")!=data.npos && (data.find("resp_")!=data.npos || data.find("response.create")!=data.npos)) {
                        ++r.hits;r.hot.insert(task.address);t0=Clock::now();capture::extract(data,r.batch,task.owned);r.parse+=seconds(t0);
                    }
                }
            }
            {std::lock_guard<std::mutex> lock(mutex);results[index]=std::move(r);++done;}end.notify_one();
        }
    }
    void enumerate(){
        if(!cached.empty() && seconds(enumerated)<0.5)return;
        auto t0=Clock::now();cached.clear();regions=0;
        uintptr_t p=0;MEMORY_BASIC_INFORMATION mbi{};
        while(VirtualQueryEx(process,reinterpret_cast<void*>(p),&mbi,sizeof(mbi))==sizeof(mbi)){
            auto base=reinterpret_cast<uintptr_t>(mbi.BaseAddress);size_t size=mbi.RegionSize;
            if(!size || base+size<=p)break;
            if(mbi.State==MEM_COMMIT && (mbi.Type==MEM_PRIVATE||mbi.Type==MEM_MAPPED) && !(mbi.Protect&(PAGE_GUARD|PAGE_NOACCESS)) && (mbi.Protect&(PAGE_READONLY|PAGE_READWRITE|PAGE_WRITECOPY|PAGE_EXECUTE_READ|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY))){
                ++regions;
                for(size_t off=0;off<size;off+=chunk){auto own=std::min(chunk,size-off);cached.push_back({base+off,std::min(own+capture::window_size,size-off),own});}
            }
            p=base+size;
        }
        region_cost=seconds(t0);enumerated=Clock::now();
    }
public:
    Scanner(HANDLE h,unsigned workers):process(h),results(workers){for(unsigned i=0;i<workers;++i)pool.emplace_back([this,i]{worker(i);});}
    ~Scanner(){{std::lock_guard<std::mutex> lock(mutex);stopping=true;}begin.notify_all();for(auto& t:pool)t.join();CloseHandle(process);}
    bool alive(){DWORD code=0;return GetExitCodeProcess(process,&code)&&code==STILL_ACTIVE;}
    void sweep(){
        if(!alive()){std::cout<<"{\"alive\":false}"<<std::endl;return;}
        auto t0=Clock::now();enumerate();tasks=cached;
        std::stable_partition(tasks.begin(),tasks.end(),[&](const Task& t){return hot.count(t.address)>0;});
        {std::lock_guard<std::mutex> lock(mutex);next=0;done=0;++generation;}begin.notify_all();
        {std::unique_lock<std::mutex> lock(mutex);end.wait(lock,[&]{return done==pool.size();});}
        Result total;std::set<std::string> seen_res,seen_req;
        for(auto& r:results){
            total.bytes+=r.bytes;total.hits+=r.hits;total.errors+=r.errors;total.read+=r.read;total.parse+=r.parse;
            total.hot.insert(r.hot.begin(),r.hot.end());
            for(auto& row:r.batch.responses)if(seen_res.insert(capture::json(row)).second)total.batch.responses.push_back(std::move(row));
            for(auto& row:r.batch.requests)if(seen_req.insert(capture::json(row)).second)total.batch.requests.push_back(std::move(row));
        }
        hot=std::move(total.hot);
        std::cout<<"{\"alive\":true,\"bytes\":"<<total.bytes<<",\"regions\":"<<regions<<",\"workers\":"<<pool.size()
            <<",\"region_cost\":"<<region_cost<<",\"scan_cost\":"<<seconds(t0)<<",\"hit_blocks\":"<<total.hits
            <<",\"read_errors\":"<<total.errors<<",\"read_worker_seconds\":"<<total.read<<",\"parse_worker_seconds\":"<<total.parse
            <<",\"responses\":"<<capture::array(total.batch.responses)<<",\"requests\":"<<capture::array(total.batch.requests)<<"}"<<std::endl;
    }
};
int main(int argc,char** argv){
    try{
        DWORD pid=0;unsigned workers=4;bool once=false,parse_stdin=false;std::string fixture;
        for(int i=1;i<argc;++i){std::string a=argv[i];
            if(a=="--pid"&&i+1<argc)pid=std::stoul(argv[++i]);
            else if(a=="--workers"&&i+1<argc)workers=std::clamp(static_cast<unsigned>(std::stoul(argv[++i])),1u,16u);
            else if(a=="--once")once=true;
            else if(a=="--parse-stdin")parse_stdin=true;
            else if(a=="--fixture"&&i+1<argc)fixture=argv[++i];
            else{std::cerr<<"Usage: collector_native --pid N [--workers 4] [--once] | --fixture FILE\n";return 2;}
        }
        if(parse_stdin){
            _setmode(_fileno(stdin),_O_BINARY);
            std::string s((std::istreambuf_iterator<char>(std::cin)),{});capture::Batch out;capture::extract(s,out);
            std::cout<<"{\"responses\":"<<capture::array(out.responses)<<",\"requests\":"<<capture::array(out.requests)<<"}"<<std::endl;return 0;
        }
        if(!fixture.empty()){
            std::ifstream in(fixture,std::ios::binary);if(!in)return 2;
            std::string s((std::istreambuf_iterator<char>(in)),{});capture::Batch out;capture::extract(s,out);
            std::cout<<"{\"responses\":"<<capture::array(out.responses)<<",\"requests\":"<<capture::array(out.requests)<<"}"<<std::endl;return 0;
        }
        if(!pid)return 2;
        HANDLE h=OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_VM_READ,FALSE,pid);
        if(!h){std::cout<<"{\"ready\":false,\"error\":"<<GetLastError()<<"}"<<std::endl;return 1;}
        Scanner scanner(h,workers);std::cout<<"{\"ready\":true,\"protocol\":1}"<<std::endl;
        if(once){scanner.sweep();return 0;}
        std::string command;
        while(std::getline(std::cin,command)){
            if(command=="scan")scanner.sweep();
            else if(command=="alive")std::cout<<(scanner.alive()?"{\"alive\":true}":"{\"alive\":false}")<<std::endl;
            else if(command=="quit")break;
            else return 2;
        }
        return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 3;}
}
