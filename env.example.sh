# Copy to env.sh and fill in (cp env.example.sh env.sh). env.sh is in .gitignore and is never submitted.
# Snowflake needs nothing here: the code reads [connections.sakila_qa_app] in ~/.snowflake/config.toml
# and signs in with the private key that file points to.
export SNOWFLAKE_CONNECTION="sakila_qa_app"
export DEEPSEEK_API_KEY="sk-..."          # platform.deepseek.com -> API Keys
# export DEEPSEEK_MODEL="deepseek-flash"  # optional; deepseek-v4-pro is used automatically when flash is busy
