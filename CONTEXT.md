# cc-session-control

This context defines the operator language for managing Claude Code sessions,
agents, and Remote Control environments from one local machine.

## Language

**Local Global Workbench**:
A machine-wide management surface for seeing and acting on Claude Code sessions,
agents, and Remote Control environments across projects.
_Avoid_: current project view, current session view

**Claude Code Session**:
A resumable Claude Code conversation or execution context whose state may be
visible through transcripts, agent listings, Remote Control, or background
execution surfaces. The session is the durable record; agents and runtimes are
ways that record is or was being executed.
_Avoid_: chat, transcript file

**Agent**:
A Claude Code execution entry that may run interactively, in the background, or
under a managed lifecycle separate from a plain terminal session. An agent is a
runtime or lifecycle wrapper for a Claude Code session, not a separate durable
work unit.
_Avoid_: process, task

**Remote Control Environment**:
A Claude Code environment exposed to mobile or web clients for controlling local
work from outside the terminal. Remote Control is a core user need; whether it
is modeled as a standalone resource or as exposure on a session depends on what
Claude Code exposes reliably on the local machine.
_Avoid_: tmux window, server process

**Live Session**:
A Claude Code session or agent that currently has an active local runtime and
can be unsafe to delete without first stopping or detaching it.
_Avoid_: existing transcript, recent session
