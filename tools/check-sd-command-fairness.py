#!/usr/bin/env python3
"""Exercise the real USB command pump with requests queued during processing."""

from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "sdexport.cpp").read_text()
start = source.index("void processSDExport()")
opening = source.index("{", start)
depth, end = 1, opening + 1
while depth:
    depth += (source[end] == "{") - (source[end] == "}")
    end += 1
pump = source[start:end]
code = r'''
#include <cstring>
#include <string>
#include <iostream>
struct Flood {};
struct SerialPort {
 std::string input="FSD 1 LIST /DATA 0\nFSD 1 LIST /DATA 0\n";
 size_t at=0;
 bool available(){return at<input.size();}
 char read(){return input[at++];}
} Serial;
int commands=0;
void request(char*) {
 if(++commands>100)throw Flood{};
 Serial.input+="FSD 1 LIST /DATA 0\n";
}
'''
code += pump + r'''
int main(){
 try{processSDExport();}catch(Flood&){}
 bool pass=commands==1&&Serial.available();
 std::cout<<"queued retries: processed="<<commands<<" remaining="<<Serial.available()
  <<" "<<(pass?"PASS":"FAIL")<<"\n";
 return !pass;
}
'''
with tempfile.TemporaryDirectory(prefix="freematics-usb-fairness-") as directory:
    cpp = Path(directory) / "fairness.cpp"
    binary = Path(directory) / "fairness"
    cpp.write_text(code)
    subprocess.run(["g++", "-std=c++17", str(cpp), "-o", str(binary)], check=True)
    raise SystemExit(subprocess.run([str(binary)], check=False).returncode)
