# core/orchestrator.py
"""
Main orchestrator that coordinates the entire workflow
"""

from datetime import datetime
from typing import Dict, Any, TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from github.file_downloader import GitHubFileDownloader
from agents.code_analyzer import CodeAnalyzer
from agents.business_translator import BusinessTranslator
from feedback.collector import FeedbackCollector
from models.ollama_client import OllamaClient
from utils.logger import logger
from utils.cache import cache

from utils.db_client import DatabaseClient


class ReportWorkflowState(TypedDict, total=False):
    repo_url: str
    id_project: int
    files_to_download: list
    started_at: datetime
    downloaded_files: Dict[str, Any]
    file_analyses: Dict[str, Any]
    business_report: Dict[str, Any]
    html_report: str

class BusinessReportOrchestrator:
    """
    Orchestrates the complete workflow:
    1. Download files from GitHub
    2. Analyze each file
    3. Translate to business language
    4. Generate beautiful report
    5. Save to database
    """
    
    def __init__(self):
        self.downloader = GitHubFileDownloader()
        self.analyzer = CodeAnalyzer()
        self.translator = BusinessTranslator()
        self.ollama = OllamaClient()
        self.feedback_collector = FeedbackCollector()
        self.db_client = DatabaseClient()

        # LangChain runnables provide a consistent invocation boundary for
        # agents. LangGraph passes each agent's output to the next node as state.
        self.analysis_runnable = RunnableLambda(self.analyzer.analyze_file)
        self.translation_runnable = RunnableLambda(
            lambda state: self.translator.translate(state["file_analyses"])
        )
        self.workflow = self._build_workflow()

        # In-memory state for the last run (simple persistence)
        self.state: Dict[str, Any] = {}
        self.last_analyses: Dict[str, Any] = {}
        self.last_business_report: Dict[str, Any] = {}
    
    async def generate_report(self, repo_url: str, id_project: int, files_to_download: list = None):
        """
        Run the LangGraph workflow and return the generated HTML report.
        """
        if not files_to_download:
            files_to_download = [
                "README.md", "app.py", "main.py", "routes.py", "models.py",
                "templates/index.html", "templates/login.html",
                "static/js/main.js", "static/css/style.css"
            ]

        result = await self.workflow.ainvoke({
            "repo_url": repo_url,
            "id_project": id_project,
            "files_to_download": files_to_download,
            "started_at": datetime.now(),
        })
        return result["html_report"]

    def _build_workflow(self):
        graph = StateGraph(ReportWorkflowState)
        graph.add_node("fetch", self._fetch_node)
        graph.add_node("analyze", self._analyze_node)
        graph.add_node("translate", self._translate_node)
        graph.add_node("report", self._report_node)
        graph.add_edge(START, "fetch")
        graph.add_edge("fetch", "analyze")
        graph.add_edge("analyze", "translate")
        graph.add_edge("translate", "report")
        graph.add_edge("report", END)
        return graph.compile()

    async def _fetch_node(self, state: ReportWorkflowState) -> Dict[str, Any]:
        repo_url = state["repo_url"]
        logger.info(f"🚀 Starting report workflow for {repo_url}")
        repo_info = self.downloader.parse_github_url(repo_url)
        logger.info(f"📦 Repository: {repo_info['owner']}/{repo_info['repo']}")
        logger.info(f"📥 Downloading {len(state['files_to_download'])} files...")
        async with self.downloader as downloader:
            downloaded_files = await downloader.download_multiple_files(
                repo_info["owner"], repo_info["repo"], state["files_to_download"]
            )
        logger.info(f"✅ Downloaded {len(downloaded_files)} files")
        return {"downloaded_files": downloaded_files}

    def _analyze_node(self, state: ReportWorkflowState) -> Dict[str, Any]:
        logger.info("🔍 Analyzing files...")
        analyses = {
            path: self.analysis_runnable.invoke(file_info)
            for path, file_info in state["downloaded_files"].items()
        }
        self.last_analyses = analyses
        return {"file_analyses": analyses}

    def _translate_node(self, state: ReportWorkflowState) -> Dict[str, Any]:
        logger.info("📝 Translating analysis into a report...")
        report = self.translation_runnable.invoke({
            "file_analyses": state["file_analyses"]
        })
        self.last_business_report = report
        return {"business_report": report}

    async def _report_node(self, state: ReportWorkflowState) -> Dict[str, Any]:
        repo_url = state["repo_url"]
        started_at = state["started_at"]
        files = state["downloaded_files"]
        analyses = state["file_analyses"]
        report = state["business_report"]
        html_report = await self._generate_html_report(
            repo_url, files, analyses, report, started_at
        )
        text_report = self._generate_text_report(
            repo_url, files, analyses, report, started_at
        )
        processing_time = (datetime.now() - started_at).total_seconds()
        self.db_client.save_report({
            "report_content": text_report,
            "attachment": html_report,
            "summary": report.get("summary", ""),
            "features_count": len(report.get("features", [])),
            "business_rules_count": len(report.get("business_logic", [])),
            "files_analyzed": len(files),
            "repo_url": repo_url,
            "processing_time_seconds": processing_time,
            "confidence_score": report.get("confidence", 0.85),
            "id_project": state["id_project"],
        })
        self._print_summary(report, len(files), started_at)
        return {"html_report": html_report}

    def ingest_feedback(self, message: str, structured: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Accept a user's feedback message (free-text) plus optional structured
        corrections, normalize via FeedbackCollector, route to the
        CodeAnalyzer for lightweight application, and persist the result.
        Returns a reconciliation summary dict.
        """
        # Normalize the incoming feedback
        feedback = self.feedback_collector.ingest_user_feedback(message, structured)

        # Process feedback against last analyses
        result = self.analyzer.process_feedback(feedback, self.last_analyses)

        # Persist feedback and result to in-memory state and cache
        self.state['feedback'] = feedback
        self.state['feedback_result'] = result
        try:
            cache.set('last_feedback', {'feedback': feedback, 'result': result}, ttl=3600)
        except Exception:
            # cache is optional — don't fail if persistence isn't available
            logger.debug('Could not persist feedback to cache')

        logger.info(f"📝 Ingested feedback: {result.get('summary')}")
        return result
    
    async def _generate_html_report(self, repo_url, files, analyses, business_report, start_time):
        """Generate beautiful HTML report"""
        
        # Count business items
        features_count = len(business_report.get('features', []))
        ui_count = len(business_report.get('user_interfaces', []))
        rules_count = len(business_report.get('business_logic', []))
        
        # Build features HTML
        features_html = ""
        for feature in business_report.get('features', [])[:5]:
            features_html += f"""
            <div class="feature-item">
                <span class="feature-icon">✨</span>
                <span>{feature}</span>
            </div>
            """
        
        # Build UI HTML
        ui_html = ""
        for ui in business_report.get('user_interfaces', [])[:5]:
            ui_html += f"""
            <div class="feature-item">
                <span class="feature-icon">🖥️</span>
                <span>{ui}</span>
            </div>
            """
        
        # Build business rules HTML
        rules_html = ""
        for rule in business_report.get('business_logic', [])[:5]:
            if isinstance(rule, dict):
                rules_html += f"""
                <div class="feature-item">
                    <span class="feature-icon">🧮</span>
                    <span><strong>Rule:</strong> {rule.get('description', '')[:100]}</span>
                </div>
                """
        
        processing_time = (datetime.now() - start_time).total_seconds()
        
        return f"""
<!DOCTYPE html>
<html>
<head>
    <title>📊 Business Impact Report</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .container {{
            background: white;
            border-radius: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
        }}
        .header h1 {{
            font-size: 2.5em;
            margin: 0;
            font-weight: 300;
        }}
        .header h1 strong {{
            font-weight: 700;
        }}
        .header .repo {{
            font-size: 1.2em;
            opacity: 0.9;
            margin-top: 10px;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            padding: 30px;
            background: #f8fafc;
        }}
        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            text-align: center;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05);
        }}
        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
        }}
        .stat-label {{
            color: #666;
            font-size: 0.9em;
            margin-top: 5px;
        }}
        .section {{
            padding: 30px;
            border-bottom: 1px solid #e0e0e0;
        }}
        .section-title {{
            font-size: 1.5em;
            color: #333;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .section-title::after {{
            content: '';
            flex: 1;
            height: 2px;
            background: linear-gradient(90deg, #667eea, transparent);
        }}
        .feature-item {{
            padding: 15px;
            background: #f8fafc;
            border-radius: 8px;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 10px;
            border-left: 3px solid #667eea;
        }}
        .feature-icon {{
            font-size: 1.2em;
        }}
        .summary-box {{
            background: linear-gradient(135deg, #f5f7fa 0%, #e4e8f0 100%);
            padding: 25px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-size: 1.1em;
        }}
        .file-list {{
            background: #f8fafc;
            padding: 20px;
            border-radius: 8px;
        }}
        .file-item {{
            padding: 8px;
            border-bottom: 1px solid #e0e0e0;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .file-item:last-child {{
            border-bottom: none;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.8em;
            font-weight: bold;
            margin-left: 10px;
        }}
        .badge-python {{ background: #3776ab; color: white; }}
        .badge-js {{ background: #f7df1e; color: black; }}
        .badge-html {{ background: #e34c26; color: white; }}
        .badge-css {{ background: #264de4; color: white; }}
        .badge-docs {{ background: #28a745; color: white; }}
        .feedback-box {{
            background: linear-gradient(135deg, #f0f4ff 0%, #f5f0ff 100%);
            padding: 20px;
            border-radius: 8px;
            border: 2px solid #e0e4ff;
        }}
        .footer {{
            text-align: center;
            padding: 30px;
            color: #666;
            background: #f8fafc;
        }}
        .timestamp {{
            color: #999;
            font-size: 0.9em;
            margin-top: 10px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 <strong>Business Impact</strong> Report</h1>
            <p class="repo">🔗 {repo_url}</p>
            <p class="timestamp">Generated in {processing_time:.1f} seconds</p>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{len(files)}</div>
                <div class="stat-label">Files Analyzed</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{features_count}</div>
                <div class="stat-label">New Features</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{ui_count}</div>
                <div class="stat-label">UI Components</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{rules_count}</div>
                <div class="stat-label">Business Rules</div>
            </div>
        </div>
        
        <div class="section">
            <div class="section-title">📋 Executive Summary</div>
            <div class="summary-box">
                {business_report.get('summary', 'Analysis complete. See details below.')}
            </div>
        </div>
        
        <div class="section">
            <div class="section-title">✨ What's New</div>
            {features_html if features_html else '<p>No new features detected in this update.</p>'}
        </div>
        
        <div class="section">
            <div class="section-title">🖥️ User Interface Updates</div>
            {ui_html if ui_html else '<p>No UI changes detected.</p>'}
        </div>
        
        <div class="section">
            <div class="section-title">🧮 Business Logic</div>
            {rules_html if rules_html else '<p>No new business rules detected.</p>'}
        </div>
        
        <div class="section">
            <div class="section-title">📁 Files Analyzed</div>
            <div class="file-list">
"""

        # Add file list
        for path, analysis in analyses.items():
            file_type = analysis.get('type', 'unknown')
            badge_class = {
                'python': 'badge-python',
                'javascript': 'badge-js',
                'html': 'badge-html',
                'css': 'badge-css',
                'documentation': 'badge-docs'
            }.get(file_type, '')
            
            html_report += f"""
                <div class="file-item">
                    <span>📄 {path}</span>
                    <span class="badge {badge_class}">{file_type}</span>
                </div>
            """
        
        html_report += f"""
            </div>
        </div>

        <div class="section">
            <div class="section-title">💬 Feedback & Improvements</div>
            <div class="feedback-box">
                <p style="color: #666; margin-bottom: 15px;">Found something inaccurate? Help us improve the analysis!</p>
                <textarea id="feedback-input" placeholder="Example: 'function calculate_discount should apply 15% discount on orders over $100'&#10;&#10;Or list specific corrections like:&#10;- function_name: new_or_corrected_purpose&#10;- another_function: fixed_description" style="width: 100%; height: 120px; padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-family: monospace; font-size: 0.9em;"></textarea>
                <button id="submit-feedback-btn" onclick="submitFeedback()" style="margin-top: 10px; padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 5px; cursor: pointer; font-weight: bold;">Submit Feedback</button>
                <div id="feedback-status" style="margin-top: 10px; color: #666; font-size: 0.9em;"></div>
            </div>
        </div>

        <div class="footer">
            <p>🤖 Generated by AI Agents • DeepSeek + Ollama</p>
            <p>This report translates code changes into business value</p>
            <p style="font-size: 0.85em; color: #999; margin-top: 10px;">💡 Tip: Use the feedback box above to help us improve future analyses!</p>
        </div>
    </div>

    <script>
        function submitFeedback() {{
            const feedbackText = document.getElementById('feedback-input').value.trim();
            const statusDiv = document.getElementById('feedback-status');
            
            if (!feedbackText) {{
                statusDiv.textContent = '⚠️ Please enter feedback before submitting.';
                statusDiv.style.color = '#ff9800';
                return;
            }}
            
            statusDiv.textContent = '📤 Submitting feedback...';
            statusDiv.style.color = '#667eea';
            
            // In a real app, this would POST to your backend
            // For now, show instructions
            setTimeout(() => {{
                statusDiv.innerHTML = '✅ Feedback captured! Pass this to the code analyzer:<br><code style="background: #f0f0f0; padding: 5px; display: block; margin-top: 5px; word-break: break-all;">' + feedbackText + '</code>';
                statusDiv.style.color = '#4caf50';
            }}, 500);
        }}
    </script>
</body>
</html>
"""
    
    def _generate_text_report(self, repo_url, files, analyses, business_report, start_time):
        """Create a compact plain-text version of the business report.
        This is what we persist into the `report.report_content` column.
        """
        processing_time = (datetime.now() - start_time).total_seconds()
        lines = []
        lines.append(f"Business Impact Report — {repo_url}")
        lines.append(f"Generated: {datetime.now().isoformat()}")
        lines.append(f"Processing time: {processing_time:.1f}s")
        lines.append("")

        # Executive summary
        lines.append("Executive summary:")
        lines.append(business_report.get('summary', 'Analysis complete.'))
        lines.append("")

        # Top features
        if business_report.get('features'):
            lines.append("Features:")
            for feat in business_report.get('features', [])[:20]:
                lines.append(f"- {feat}")
            lines.append("")

        # UI items
        if business_report.get('user_interfaces'):
            lines.append("UI changes:")
            for ui in business_report.get('user_interfaces', [])[:20]:
                lines.append(f"- {ui}")
            lines.append("")

        # Business rules
        if business_report.get('business_logic'):
            lines.append("Business rules:")
            for rule in business_report.get('business_logic', [])[:20]:
                if isinstance(rule, dict):
                    lines.append(f"- {rule.get('description', '')}")
                else:
                    lines.append(f"- {rule}")
            lines.append("")

        # Files analyzed
        lines.append(f"Files analyzed ({len(files)}):")
        for path, analysis in analyses.items():
            lines.append(f"- {path} ({analysis.get('type','unknown')})")

        return "\n".join(lines)

    def _print_summary(self, business_report, files_count, start_time):
        """Print a quick console summary"""
        processing_time = (datetime.now() - start_time).total_seconds()
        
        print("\n" + "="*60)
        print("📊 BUSINESS IMPACT SUMMARY")
        print("="*60)
        print(f"📁 Files analyzed: {files_count}")
        print(f"⏱️  Processing time: {processing_time:.1f}s")
        print("\n✨ Key Business Impact:")
        
        for feature in business_report.get('features', [])[:3]:
            print(f"   • {feature}")
        for ui in business_report.get('user_interfaces', [])[:3]:
            print(f"   • {ui}")
        for rule in business_report.get('business_logic', [])[:3]:
            if isinstance(rule, dict):
                print(f"   • {rule.get('description', '')[:80]}")
        
        print("\n" + "="*60)
