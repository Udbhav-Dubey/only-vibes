#include "HttpResponse.h"
#include "Router.h"
#include "Songs.h"
#include "utils.h"
#include <iostream>
#include <filesystem>
#include <algorithm>
#include <cctype>
#include <vector>
#include <string>
namespace fs=std::filesystem;

Router::Router(SongLibrary &lib):library(lib){}

// returns true and fills id if path looks like <prefix><number>
static bool parseId(const std::string &path,const std::string &prefix,int &id){
    if (path.rfind(prefix,0)!=0||path.length()<=prefix.length()) return false;
    try{
        id=std::stoi(path.substr(prefix.length()));
        return true;
    }
    catch(const std::exception &){
        return false;
    }
}

static bool serveIndex(const std::string &dir,HttpResponse &out){
    std::string html;
    if (!FileReader::read(dir+"/index.html",html,false)) return false;
    out=HttpResponse(200,"text/html");
    out.SetBody(html);
    return true;
}

HttpResponse Router::route(const std::string &path){
    int id=0;

    if (path=="/api/songs"){
        return HttpResponse::json(library.ToJson());
    }

    if (parseId(path,"/api/play/",id)||parseId(path,"/play/",id)){
        const Song *song=library.getSong(id);
        std::string data;
        if (song&&FileReader::read(song->getmp3_file(),data,true)){
            HttpResponse resp(200,"audio/mpeg");
            resp.SetBody(data);
            return resp;
        }
        return HttpResponse::notfound();
    }

    if (parseId(path,"/lyrics/",id)){
        const Song *song=library.getSong(id);
        std::string data;
        if (song&&!song->getlyrics_file().empty()&&FileReader::read(song->getlyrics_file(),data,false)){
            HttpResponse resp(200,"text/plain");
            resp.SetBody(data);
            return resp;
        }
        return HttpResponse::notfound();
    }

    if (parseId(path,"/download/",id)){
        const Song *song=library.getSong(id);
        std::string data;
        if (song&&FileReader::read(song->getmp3_file(),data,true)){
            HttpResponse resp(200,"audio/mpeg");
            resp.setContentDisposition(fs::path(song->getmp3_file()).filename().string());
            resp.SetBody(data);
            return resp;
        }
        return HttpResponse::notfound();
    }

    if (path=="/"||path=="/index.html"){
        HttpResponse r(200,"text/html");
        if (serveIndex(".",r)||serveIndex("..",r)) return r;

        std::string page="<html><body style='font:20px sans-serif;padding:40px'><h1>Pick a frontend</h1>";
        std::vector<std::string> dirs;
        for (const auto &e:fs::directory_iterator(".")){
            if (e.is_directory()&&fs::exists(e.path()/"index.html"))
                dirs.push_back(e.path().filename().string());
        }
        std::sort(dirs.begin(),dirs.end());
        for (const auto &d:dirs) page+="<p><a href='/"+d+"'>"+d+"</a></p>";
        page+="</body></html>";
        r.SetBody(page);
        return r;
    }

    // "/claude", "/chatgpt/", "/gemini" -> <folder>/index.html
    std::string name=path.size()>1?path.substr(1):"";
    if (!name.empty()&&name.back()=='/') name.pop_back();
    bool safe=!name.empty()&&std::all_of(name.begin(),name.end(),
        [](unsigned char c){return std::isalnum(c)||c=='-'||c=='_';});
    if (safe){
        HttpResponse r(200,"text/html");
        if (serveIndex(name,r)) return r;
    }

    return HttpResponse::notfound();
}
