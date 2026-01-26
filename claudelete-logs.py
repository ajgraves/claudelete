#!/usr/bin/env python3
import cgi
import subprocess
import sys
import os

# ────────────────────────────────────────────────
#          CONFIGURATION ── CHANGE THESE
# ────────────────────────────────────────────────
PASSWORD         = "YOUR SUPER SECRET PASSWORD HERE"
REFRESH_INTERVAL = 30          # in seconds
# ────────────────────────────────────────────────

# Function to run the command and get output
def get_log_output():
    try:
        result = subprocess.run(
            "journalctl --user-unit claudelete-bot -n 2000 --no-pager | tac",
            shell=True,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"Error running command: {e.stderr}"

# Parse query string
form = cgi.FieldStorage()
action = form.getvalue('action')

if action == 'get_log':
    print("Content-Type: text/plain\n")
    print(get_log_output())
else:
    print("Content-Type: text/html\n")
    print("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log Viewer</title>
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <style>
        :root {
            --bg-color: #ffffff;
            --text-color: #000000;
            --log-bg: #f8f8f8;
            --log-border: #ccc;
            --timer-color: #555;
            --heading-color: #0066cc;
        }
        body.dark {
            --bg-color: #0d1117;
            --text-color: #c9d1d9;
            --log-bg: #161b22;
            --log-border: #30363d;
            --timer-color: #8b949e;
            --heading-color: #58a6ff;
        }
        body {
            font-family: monospace;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 20px;
            transition: background-color 0.3s, color 0.3s;
        }
        #log-output {
            white-space: pre-wrap;
            background-color: var(--log-bg);
            border: 1px solid var(--log-border);
            padding: 12px;
            max-height: 80vh;
            overflow-y: auto;
            border-radius: 6px;
        }
        h1 {
            color: var(--heading-color);
            margin-top: 0;
        }
        #timer {
            margin-top: 8px;
            font-size: 0.9em;
            color: var(--timer-color);
        }
        #theme-toggle {
            position: fixed;
            top: 15px;
            right: 20px;
            padding: 8px 12px;
            background: var(--log-bg);
            border: 1px solid var(--log-border);
            color: var(--text-color);
            cursor: pointer;
            border-radius: 4px;
            font-size: 1.2em;
            line-height: 1;
            min-width: 44px;
            text-align: center;
            z-index: 10;
        }
        #theme-toggle:hover {
            opacity: 0.9;
        }
        /* Scrollbar styling */
        #log-output::-webkit-scrollbar { width: 8px; }
        #log-output::-webkit-scrollbar-track { background: var(--bg-color); }
        #log-output::-webkit-scrollbar-thumb { background: var(--log-border); border-radius: 4px; }
        #log-output::-webkit-scrollbar-thumb:hover { background: #484f58; }
    </style>
</head>
<body>
    <div id="content" style="display: none;">
        <button id="theme-toggle" aria-label="Toggle theme">🌙</button>
        <h1>Claudelete Bot Logs</h1>
        <div id="log-output">Loading logs...</div>
        <div id="timer">Next refresh in %d seconds</div>
    </div>

    <script>
        const PASSWORD = "%s";
        const REFRESH_INTERVAL = %d;

        let countdown;

        function startCountdown() {
            let timeLeft = REFRESH_INTERVAL;
            const timerEl = document.getElementById('timer');

            clearInterval(countdown);

            countdown = setInterval(() => {
                timeLeft--;
                timerEl.textContent = `Next refresh in ${timeLeft} second${timeLeft !== 1 ? 's' : ''}`;

                if (timeLeft <= 0) {
                    clearInterval(countdown);
                    timerEl.textContent = "Refreshing...";
                }
            }, 1000);
        }

        function checkPassword() {
            const userInput = prompt("Enter password to access logs:");
            if (userInput === PASSWORD) {
                document.getElementById('content').style.display = 'block';
                initTheme();
                loadLogs();
            } else {
                alert("Incorrect password!");
                checkPassword(); // retry
            }
        }

        function loadLogs() {
            $.ajax({
                url: window.location.pathname + '?action=get_log',
                type: 'GET',
                success: function(data) {
                    $('#log-output').text(data);
                    startCountdown();
                    setTimeout(loadLogs, REFRESH_INTERVAL * 1000);
                },
                error: function() {
                    $('#log-output').text('Error loading logs.');
                    document.getElementById('timer').textContent = "Error — will retry soon";
                    setTimeout(loadLogs, REFRESH_INTERVAL * 1000);
                }
            });
        }

        // Theme handling
        const themeToggle = document.getElementById('theme-toggle');
        const body = document.body;

        function setTheme(theme) {
            if (theme === 'dark') {
                body.classList.add('dark');
                themeToggle.textContent = '☀️';
                themeToggle.setAttribute('aria-label', 'Switch to light mode');
            } else {
                body.classList.remove('dark');
                themeToggle.textContent = '🌙';
                themeToggle.setAttribute('aria-label', 'Switch to dark mode');
            }
            localStorage.setItem('theme', theme);
        }

        function initTheme() {
            const savedTheme = localStorage.getItem('theme');
            if (savedTheme) {
                setTheme(savedTheme);
            } else {
                const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                setTheme(prefersDark ? 'dark' : 'light');
            }
        }

        themeToggle.addEventListener('click', () => {
            const current = body.classList.contains('dark') ? 'dark' : 'light';
            setTheme(current === 'dark' ? 'light' : 'dark');
        });

        window.onload = checkPassword;
    </script>
</body>
</html>
    """ % (REFRESH_INTERVAL, PASSWORD, REFRESH_INTERVAL))