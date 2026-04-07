@echo off
cd /d "%~dp0"
set AGT_DECK_TOKEN=agt_deck_local_dev_token_change_me_2026
echo Starting AGT Command Deck...
python -m agt_deck.main
pause