/******************************************************************************
* Web based vehicle tracker based on Freematics Hub
* Developed by Stanley Huang https://www.facebook.com/stanleyhuangyc
* Distributed under BSD license
* Visit http://freematics.com/hub/api for Freematics Hub API reference
* To obtain your Freematics Hub server key, contact support@freematics.com.au
* 
* THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
* IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
* FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
* AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
* LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
* OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
* THE SOFTWARE.
******************************************************************************/

onResize();

$(function () {
    $(document).ready(function () {
		USER.load("DASH.load()");

        self.setInterval(function () {
            if (DASH.curLocation && OSMAP.map) OSMAP.setCenter(DASH.curLocation);
        }, MAP_CENTERING_INTERVAL);
    });
});

if (window.require) {
    const shell = require('electron').shell;

    function openLink(url) {
        shell.openExternal(url);
    }
}

function onResize()
{
    var height = window.innerHeight;
	var width = window.innerWidth;
    //if (mapHeight < 300) mapHeight = 300;
	if (width > height) {
		document.getElementById("container").style.height = height + "px";
		document.getElementById("map").style.height = height / 2 + "px";
		document.getElementById("info").style.height = height / 2 + "px";
	} else {
		document.getElementById("container").style.height = height + "px";
		document.getElementById("map").style.height = height * 3 / 4 + "px";
		document.getElementById("info").style.height = height / 4 + "px";
	}
}

var DASH = {
	xhr: new XMLHttpRequest(),
    deviceID: null,
    curLocation: null,
    data: null,
    updateUserInfo: function (info, devid)
    {
        var container = document.getElementById("userinfo");
        container.textContent = "";
        if (!USER.info) {
            var home = document.createElement("input");
            home.type = "button";
            home.value = "Home";
            home.onclick = function () { location.href = "/"; };
            container.appendChild(home);
            return;
        }
        var select = document.createElement("select");
        select.onchange = function () { USER.goDash(select.value); };
        var found = false;
        for (var i = 0; i < info.devid.length; i++) {
            var option = document.createElement("option");
            option.value = info.devid[i];
            option.textContent = info.devid[i];
            if (info.devid[i] == devid) {
                option.selected = true;
                found = true;
            }
            select.appendChild(option);
        }
        if (!found) {
            var fallback = document.createElement("option");
            fallback.value = devid;
            fallback.textContent = devid;
            fallback.selected = true;
            select.appendChild(fallback);
        }
        container.appendChild(select);
        var switchButton = document.createElement("input");
        switchButton.type = "button";
        switchButton.value = "Switch";
        switchButton.onclick = function () { USER.goNextDash(select.value); };
        container.appendChild(switchButton);
        var homeButton = document.createElement("input");
        homeButton.type = "button";
        homeButton.value = "Home";
        homeButton.onclick = function () { location.href = "/"; };
        container.appendChild(homeButton);
    },
	setText: function(name, text)
	{
        document.getElementById(name).innerText = text;
    },
    setClass: function (name, className)
    {
        document.getElementById(name).className = className;
    },
	getPIDValue: function (pid)
	{
		for (var i = 0; i < this.data.length; i++) {
			if (this.data[i][0] == pid) {
				return PID.normalize(pid, this.data[i][1]);
			}
		}
		return null;
	},
	update: function (ch)
	{
        var parked = ch.parked || ch.age.data > TRIP_END_TIMEOUT;
        if (parked) {
            this.setText("elapsed", getHHMM(Math.floor(ch.age.data / 1000)));
            this.setText("state", "PARKED");
            this.setClass("state", "state_parked");
        } else {
            this.setText("elapsed", getHHMMSS(ch.elapsed));
            this.setText("state", "RUNNING");
            this.setClass("state", "state_running");
        }
		
        this.data = ch.data;

	    // update map
        var lat = this.getPIDValue(PID.GPS.LATITUDE);
        var lng = this.getPIDValue(PID.GPS.LONGITUDE);
        if (lat != null && lng != null && lat != 0 && lng != 0) {
            if (!OSMAP.map) OSMAP.init("map", lat, lng, 20);
		    //if (devid) OSMAP.setTooltip(0, devid);
            if (!this.curLocation || this.curLocation[0] != lat || this.curLocation[1] != lng) {
                this.curLocation = [lat, lng];
                OSMAP.setMarker(0, this.curLocation);
                OSMAP.setCenter(this.curLocation);
            }
        }
	},
	load: function()
    {
		this.updateUserInfo(USER.info, USER.devid);
		this.reload();
    },
	reload: function()
	{
		// load channel data
		this.xhr.onreadystatechange = function() {
			if (this.readyState != 4) return;
			if (this.status != 200) {
				if (this.status) {
					alert("Server under maintenance (status: " + this.status + ")");
				}
				return;
			}
            var chdata = JSON.parse(this.responseText);
            if (chdata && chdata.id) {
                DASH.deviceID = chdata.devid;
				DASH.update(chdata);
				self.setTimeout("DASH.reload()", chdata.parked ? DATA_FETCH_INTERVAL_PARKED : DATA_FETCH_INTERVAL);
            } else {
                alert("Not an active device. Please check if your device is working and the device ID is correct.");
            }
		};
        var url = serverURL + "channels/" + USER.devid + "?data=1";
		this.xhr.open('GET', url, true);    
		this.xhr.send(null);
	}
};
