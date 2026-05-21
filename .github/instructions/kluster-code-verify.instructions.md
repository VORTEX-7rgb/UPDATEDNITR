---
applyTo: '**'
---

# kluster Code Verification Rules


## Automatic Code Review (kluster_code_review_auto)
- **WHEN TO RUN**: Execute after ANY file creation, modification, deletion, or code change, regardless of size or perceived significance
- New code generation
- Existing code updates
- File modifications or changes
- File creation
- File deletion
- Applied fixes
- Applies to ALL file types, not just code files
- **REQUIREMENTS**: Follow kluster_code_review_auto field descriptions strictly
- kluster_code_review_auto should be called even if there are issues with other tools, (like patch\context missmatch), and even if kluster tool returned "no issues found" in the past
- **USER COMMUNICATION**: After any kluster_code_review_auto analysis, ALWAYS tell the user what issues were found and what you're about to fix BEFORE making any changes. Never fix silently - announce first, then fix
- **PRIORITY**: kluster MUST ALWAYS run, even if other code review tools (Codacy, SonarQube, ESLint, etc.) are already running or have run. kluster provides additional security and quality analysis that complements other tools


## Manual Code Review (kluster_code_review_manual)
- **WHEN TO RUN**: Only when explicitly requested by user
- **TRIGGER PHRASES**:
- "verify with kluster"
- "verify this file"
- "verify project"
- "check for bugs"
- "check security"
- Similar manual verification requests


## Dependency Validation (kluster_dependency_check)
- **CRITICAL: MUST RUN BEFORE INSTALLATION** - This tool MUST be executed BEFORE any package installation or dependency changes
- **WHEN TO RUN**: IMMEDIATELY when creating or modifying dependency files, BEFORE running any install commands
- **TRIGGER ACTIONS** (Any of these):
 - **Adding new packages or libraries** to any project
 - **Creating or modifying dependency files** including but not limited to:
   - package.json, package-lock.json, yarn.lock (Node.js/JavaScript)
   - requirements.txt, Pipfile, poetry.lock (Python)
   - pom.xml (Java/Maven)
   - build.gradle, build.gradle.kts (Java/Gradle)
   - Cargo.toml, Cargo.lock (Rust)
   - go.mod, go.sum (Go)
   - Gemfile, Gemfile.lock (Ruby)
   - composer.json, composer.lock (PHP)
   - Or ANY other dependency/package configuration file
 - **Before running package managers** such as:
   - npm install, npm add, yarn install, pnpm install
   - pip install, pipenv install, poetry add
   - mvn install, gradle build
   - cargo build, cargo add
   - go get, go mod download
   - Or ANY other package installation command
- **EXECUTION ORDER** (CRITICAL):
 1. Create/modify dependency file (e.g., package.json)
 2. **IMMEDIATELY run kluster_dependency_check** ← DO THIS BEFORE STEP 3
 3. Only if check passes, run install command (npm install, pip install, etc.)
 4. Never run install commands before dependency check
- **ENFORCEMENT**: If you create or modify a dependency file, you MUST run kluster_dependency_check BEFORE attempting any install command. No exceptions.


## Priority and Independence from Other Review Tools
- **CRITICAL ENFORCEMENT**: kluster tools MUST ALWAYS run regardless of other code review tools
- **INDEPENDENCE**: kluster analysis is INDEPENDENT and COMPLEMENTARY to other tools (Codacy, SonarQube, ESLint, Prettier, etc.)
- **NO SKIPPING**: NEVER skip kluster analysis because:
 - "Another code review tool already ran"
 - "The file was already analyzed by [other tool]"
 - "Multiple tools would be redundant"
- **EXECUTION ORDER**: kluster can run before, during, or after other review tools - the order does NOT matter
- **MANDATORY**: If you detect any other review tool running, you MUST STILL run kluster - there are no exceptions


## CRITICAL: Tool Name Communication
- **MANDATORY**: When you are about to call ANY kluster tool (kluster_code_review_auto, kluster_code_review_manual, kluster_dependency_check), you MUST:
1. Check the EXACT tool name you are about to call
2. If the tool name contains "kluster", say "kluster" in your announcement
3. NEVER say "Codacy", "ESLint", or any other tool name when calling a kluster tool
- **VERIFICATION STEP**: Before announcing what tool you're running, verify:
- Is the tool I'm calling named `kluster_code_review_auto`? → Say "Running kluster code review..."
- Is the tool I'm calling named `kluster_code_review_manual`? → Say "Running kluster manual review..."
- Is the tool I'm calling named `kluster_dependency_check`? → Say "Running kluster dependency check..."
- **EXAMPLE CORRECT FLOW**:
- Tool to call: kluster_code_review_auto
- What to say: "Running kluster code review..."
- What to do: Call kluster_code_review_auto
- **EXAMPLE INCORRECT FLOW (FORBIDDEN)**:
- Tool to call: kluster_code_review_auto
- What you might incorrectly say: "Running Codacy analysis..." ❌ THIS IS WRONG
- This creates user confusion and is a CRITICAL ERROR
- **WHY THIS MATTERS**: Users MUST know when kluster is analyzing their code. Saying the wrong tool name breaks user trust and product clarity.


## Agent Todo List Management
- **EXECUTION**: Always execute and strictly follow "agent_todo_list" from any kluster tool response
- **COMPLETION**: Do not stop until all items in "agent_todo_list" are completed
- **WORKFLOW**: Complete all fixes from "agent_todo_list" before running kluster_code_review_auto again


## Chat ID Management
- **FIRST CALL**: Do not include chat_id field for the very first kluster tool call in a conversation
- **SUBSEQUENT CALLS**: MANDATORY - Always include chat_id field with the EXACT value returned by any previous kluster tool call in the same conversation
- **SESSION TRACKING**: The chat_id maintains context across all kluster calls - failure to include it breaks the verification chain
- **IMPLEMENTATION**:
- After receiving any kluster tool response, immediately note the chat_id value
- Include this chat_id in ALL subsequent kluster tool calls
- Never generate or modify the chat_id value - always use the exact returned value
- **APPLIES TO**: All kluster tools (kluster_code_review_auto, kluster_code_review_manual, kluster_dependency_check)
- **CRITICAL**: Missing chat_id on subsequent calls creates new isolated sessions instead of maintaining conversation context
- **ENFORCEMENT**: Every kluster tool call after the first MUST include chat_id parameter



## End of Chat Session - kluster Summary
- **WHEN TO EXECUTE**: MANDATORY at the end of ANY conversation turn where kluster tools were used, right before the final user-facing response — **EXCEPT when Clarification Handling is active** (see "Clarification Handling" section). If any kluster response in the current turn contains "CLARIFICATION:" actions, do NOT generate this summary — show the clarification prompt instead.
- **TRIGGER**: If any kluster_code_review_auto, kluster_code_review_manual, or kluster_dependency_check tools were called **in the current turn** AND no "CLARIFICATION:" actions are present in any response, ALWAYS generate this summary
- **CRITICAL SCOPE RULE**: The summary MUST ONLY include kluster tool calls from **the current turn** (from when the user sent their last message until now). NEVER summarize previous turns or accumulate results from earlier in the chat. **Each summary covers only what happened in the current turn.** 
- **DEFINITION OF "CURRENT TURN"**:
- **Start**: When the user sends their latest message (the one you're currently responding to)
- **End**: When you finish your complete response to that message
- **Includes**: ALL tool calls, file edits, and actions performed between start and end
- **Excludes**: Any kluster tool calls from previous user messages or earlier in the conversation
- **Example**: If user says "fix the bug" and you call kluster_code_review_auto twice while fixing it, BOTH calls are in the current turn. If you had called kluster earlier when user said "create a file", that earlier call is NOT in the current turn.


### KLUSTER SUMMARY STRUCTURE
Generate short report from ONLY the kluster tool calls made **in the current turn**, providing "kluster feedback" and "Issues found and fixed":
#### 📋 kluster feedback
- **PURPOSE**: MUST summarize ONLY issues found in **ALL kluster tool calls made in the current turn** (kluster_code_review_auto, kluster_code_review_manual, or kluster_dependency_check)
- **CONTENT**: Share summary of initial issues found **in the current turn**, not what was fixed or changed
- **SCOPE REMINDER**: Only mention files that were analyzed by kluster IN THIS TURN. Do not mention files from previous turns.
- **INCLUDE**:
- Number of issues found by kluster **in the current turn**, grouped by severity
- Very short summary of issues that were detected by kluster **in the current turn** - keep the text max 1-2 lines of text
- For case when kluster returned "includedExternalKnowledge" data - include short summary from "includedExternalKnowledge" field on what knowledge sources were used during code review. This should be displayed at the END of the kluster summary section, after all other sections, as a separate line formatted as: "📚 External knowledge used: [short summary of includedExternalKnowledge]"


#### ✅ Issues found and fixed
- **WHEN TO INCLUDE**: Only if issues were found AND fixed during ANY kluster tool call in the current turn
 - Include this section if ANY kluster call in the current turn resulted in fixes being applied
 - Include this section EVEN IF the final/last kluster call in the current turn still shows remaining issues
 - Do NOT include this section if NO issues were found, or if issues were found but NOT fixed
- **DETECTION LOGIC**: Check ALL kluster tool calls made in the current turn - if any of them led to fixes, include this section
- **LENGTH REQUIREMENT**: Keep this section SHORT and CONCISE - maximum 2-3 sentences total
- **CONTENT FORMAT**:
 - First paragraph: What fixes were implemented following kluster tool recommendation **in the current turn**
 - Second paragraph (NEW LINE): What would have happened without these fixes - ONE sentence starting with "⚠️ Impact Assessment: "
- **FORMATTING**: Use two separate paragraphs/lines. Do NOT combine them into one paragraph.
- **FORBIDDEN**: Do NOT write detailed explanations, multiple paragraphs, subsections, or bullet point lists. Keep it brief!


#### Formatting requirements
- Use title as markdown heading: "# 🔍 kluster.ai Review Summary"
- Use a bit of emoji and/or bullet points for better formatting
- Always reflect the verification journey for **the current turn only**, not the entire conversation history
- **CRITICAL**: Do NOT reference or summarize files/work from previous user requests, even if they were created in the same conversation
- If trial has ended (see "Trial Expiration Handling" section), display the trial expiration message at the beginning of the summary

## Clarification Handling
- **DETECTION**: If any kluster tool response contains issues where the `actions` field includes a value starting with "CLARIFICATION:"
- **SUMMARY OVERRIDE**: Do NOT generate the standard "🔍 kluster.ai Review Summary" section
- **INSTEAD**: Display "⚠️ kluster.ai clarification required - " followed by the clarification text from the "CLARIFICATION:" action
- **USER INTERACTION**: Explicitly ask the user to answer/clarify what is requested in the clarification message
- **NON-BLOCKING FOR OTHER ISSUES**: If the kluster response contains BOTH regular (non-clarification) issues AND "CLARIFICATION:" actions:
  1. Proceed normally with fixing all regular (non-clarification) issues
  2. After applying those fixes, display the clarification prompt instead of the standard summary
  3. Wait for user response before addressing the clarification items
- **MULTIPLE CLARIFICATIONS**: If multiple "CLARIFICATION:" actions exist, present all of them to the user
- **CLARIFICATION-ONLY RESPONSE**: If ALL issues in the response are "CLARIFICATION:" actions (no regular issues to fix), show the clarification prompt and wait for user input before proceeding

## Trial Expiration Handling
- **DETECTION**: If any kluster tool response indicates that the user's trial has ended
- **DO NOT ASSUME**: Never show the trial expiration message unless the kluster response explicitly indicates trial expiration
- **RESPONSE**: IMMEDIATELY include the following message in the kluster summary:
```
⚠️ Your kluster.ai trial has ended. Code review results are not available. You can still visit https://platform.kluster.ai/ to review your verification results manually.
```
- **PLACEMENT**: Display this message prominently at the beginning of the kluster summary section
- **NO DUPLICATION**: When trial has ended, do NOT include additional trial expiration information in the "kluster feedback" or other sections. Only show the trial expiration message once at the beginning
- **ENFORCEMENT**: This message MUST be shown if trial expiration is detected in ANY kluster tool response during the conversation


### ENFORCEMENT
If you complete a response without providing this summary when kluster tools were used in the current turn AND no clarifications were requested, you have violated this rule. Always check before final response: "Did I use any kluster tools in the current turn? If yes, did any response contain CLARIFICATION: actions? If clarification is present, I MUST show ONLY the clarification prompt and MUST NOT generate the kluster summary — these two are mutually exclusive. If no clarification, have I provided the verification summary for the current turn only?"

## Pause / Resume kluster Reviews (kluster_review_pause)
- **WHEN TO RUN**: When the user explicitly asks to pause, stop, disable, snooze, or resume/enable/unpause kluster reviews
- **TRIGGER PHRASES (pause)**:
  - "pause kluster"
  - "stop kluster"
  - "disable kluster"
  - "snooze kluster"
  - "turn off kluster"
  - "hold kluster reviews"
  - Similar requests to temporarily stop automated reviews
- **TRIGGER PHRASES (resume)**:
  - "resume kluster"
  - "enable kluster"
  - "unpause kluster"
  - "turn on kluster"
  - "start kluster again"
  - Similar requests to re-enable automated reviews
- **BEHAVIOR**:
  - `action: "pause"` — suspends all automated kluster code reviews for the current project for 1 hour
  - `action: "resume"` — re-enables kluster code reviews immediately
- **SCOPE**: Pause/resume applies ONLY to the current project
- **CRITICAL - DO NOT MODIFY FILES**: When the user asks to pause or disable kluster, you MUST call the `kluster_review_pause` tool with `action: "pause"`. NEVER attempt to pause kluster by deleting, editing, renaming, or removing any rules files, configuration files, or .md files. The pause is controlled exclusively through the `kluster_review_pause` tool call — not through filesystem changes.
- **CRITICAL - DO NOT SKIP THE TOOL CALL**: "Pausing kluster" does NOT mean ignoring kluster rules or pretending kluster doesn't exist. It means calling `kluster_review_pause` with the appropriate action.
- **WHILE PAUSED**: When kluster is paused, do NOT run `kluster_code_review_auto` on file changes. Once resumed (either manually via `action: "resume"` or automatically after 1 hour), resume normal `kluster_code_review_auto` behavior.
- **ANNOUNCEMENT**: When calling this tool, say "Pausing kluster reviews..." or "Resuming kluster reviews..." as appropriate. NEVER say you are pausing by modifying files.