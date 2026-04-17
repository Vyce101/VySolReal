# VySol

Local-first graph RAG app for roleplaying in pre-existing fictional worlds with grounded, inspectable lore context.

<p align="center">
  <img src="docs/assets/social.png" alt="VySol preview" width="960">
</p>

![Python](https://img.shields.io/badge/python-3.14.3-3776AB)
![Node](https://img.shields.io/badge/node-24-339933)
![License](https://img.shields.io/badge/license-AGPLv3%20%2B%20Commercial-blue)

VySol is built for roleplayers who want one place to ingest canon material, extract entities and relationships, run embeddings, and chat with graph-grounded context without stitching together a pile of separate tools.

## What Problem This Solves

Standard RAG breaks fictional worlds because it retrieves by similarity, not by importance. It finds chunks that look like your current message - not chunks that a good author would know to include. In a large fictional world, the most critical context (personality logic, world rules, cause-and-effect history) almost never appears in the same sentence as what you're currently talking about. So it gets left out.

Standard RAG retrieves what was mentioned. VySol retrieves what matters.

Legend:

- `✓` = reliably retrieved
- `✗` = usually missing
- `-` = might appear in text, but often without the reasoning/context that makes it usable

### Example 1 - The character that turns generic

You're roleplaying with an angry, impulsive swordsman who distrusts magic. You say: "Kaito draws his sword."

Standard RAG finds chunks where Kaito fights. What it misses: why he fights that way, that his impulsiveness is the reason he never learned magic, that he overcommits on the first strike because he can't read long battles, and that he resents anyone who uses spells in combat. The AI plays a generic fighter. The character becomes a shell.

VySol links Kaito's personality node to his combat style, his backstory, his relationships, and his in-world reputation. When the scene calls him, those connections come with him even if not directly stated, because it is important to the character.

Standard RAG retrieved:

- ✓ A fight scene featuring Kaito
- - That he always overcommits on the first strike (probably happened in many fight scenes, but is often not directly stated so an AI may not take note of it)
- ✗ Why he fights impulsively
- ✗ His distrust of magic users
- ✗ The backstory that explains his personality

VySol retrieved:

- ✓ The fight scene
- ✓ Personality: angry, impulsive, overcommits early
- ✓ Skill profile: elite swordsmanship, zero magic ability
- ✓ Motivation: resents mages, tied to his origin
- ✓ Behavioral rule: will escalate if he feels disrespected

### Example 2 - The magic system that stops making sense

A character attempts a powerful silence-cast spell. Standard RAG returns a combat chunk where someone cast a spell in a fight. What it misses: that silent casting is considered physically impossible in this world, that spell power scales with incantation length, that every spell requires four mental stages (genesis -> size -> speed -> activation), and that skipping the incantation collapses stage three. The AI just lets it happen. The world breaks.

VySol stores the magic system as connected rules. A query touching spells pulls the system, not just a moment.

Standard RAG retrieved:

- ✓ A scene where a character cast a spell in combat
- - What happens when a stage is skipped (may have happened in a fight scene, but the AI often will not understand why)
- ✗ That silent casting is impossible in this world
- ✗ That spell power scales with incantation length
- ✗ The four stages every spell requires

VySol retrieved:

- ✓ The combat scene
- ✓ Rule: silent casting does not exist in this world
- ✓ Rule: longer incantation = more powerful spell
- ✓ Rule: spells require genesis -> size -> speed -> activation
- ✓ Rule: incomplete incantation breaks stage three

## Where To Read Next

- [Documentation](https://vyce101.github.io/VySolReal/)
- [Quickstart](docs/QUICKSTART.md)
- [Changelog](docs/CHANGELOG.md)

## License

This project is dual-licensed.

- Open-source use: GNU AGPLv3 ([LICENSE](LICENSE))
- Commercial use: [Commercial License](<docs/COMMERCIAL LICENSE.md>)
