#!/bin/bash
# ============================================================
#  RoboTank — Script d'initialisation GitHub
#  Usage : ./setup_github.sh
# ============================================================

set -e

# ── Configuration ──────────────────────────────────────────
REPO_NAME="RoboTank"
DESCRIPTION="Robot mobile de surveillance et cartographie WiFi — SAÉ6.IOM.01, IUT de Blois"

# ── Couleurs ───────────────────────────────────────────────
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     🤖 RoboTank — GitHub Setup           ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Étape 1 : Vérifier Git ────────────────────────────────
if ! command -v git &> /dev/null; then
    echo -e "${RED}❌ Git n'est pas installé.${NC}"
    echo "   → sudo apt install git"
    exit 1
fi
echo -e "${GREEN}✓ Git détecté${NC}"

# ── Étape 2 : Vérifier la config Git ──────────────────────
if [ -z "$(git config --global user.name)" ]; then
    echo -e "${YELLOW}⚠ Aucun nom Git configuré.${NC}"
    read -p "   Ton nom (ex: Léo-Paul) : " GIT_NAME
    git config --global user.name "$GIT_NAME"
fi

if [ -z "$(git config --global user.email)" ]; then
    echo -e "${YELLOW}⚠ Aucun email Git configuré.${NC}"
    read -p "   Ton email GitHub : " GIT_EMAIL
    git config --global user.email "$GIT_EMAIL"
fi
echo -e "${GREEN}✓ Config Git OK ($(git config --global user.name) <$(git config --global user.email)>)${NC}"

# ── Étape 3 : Vérifier GitHub CLI (optionnel) ─────────────
if command -v gh &> /dev/null; then
    HAS_GH=true
    echo -e "${GREEN}✓ GitHub CLI (gh) détecté${NC}"
else
    HAS_GH=false
    echo -e "${YELLOW}⚠ GitHub CLI non installé (pas grave, on fera sans)${NC}"
fi

# ── Étape 4 : Initialiser le repo ─────────────────────────
echo ""
echo -e "${CYAN}▸ Initialisation du dépôt Git...${NC}"
git init
git add .
git commit -m "🚀 Initial commit — RoboTank project

- Flask server (multithreaded, stepper control, camera stream)
- Web interface (Joystick, D-PAD, Autonomous, WiFi Scan modes)
- LaTeX technical specification document
- Robot photos, 3D design renders, UI screenshots
- Project documentation and README"

echo -e "${GREEN}✓ Premier commit créé${NC}"

# ── Étape 5 : Créer le repo sur GitHub ────────────────────
echo ""
if [ "$HAS_GH" = true ]; then
    echo -e "${CYAN}▸ Création du repo GitHub via 'gh'...${NC}"
    gh repo create "$REPO_NAME" \
        --public \
        --description "$DESCRIPTION" \
        --source=. \
        --remote=origin \
        --push
    echo -e "${GREEN}✓ Repo créé et code poussé !${NC}"
else
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  ÉTAPES MANUELLES (sans GitHub CLI) :${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  1. Va sur ${CYAN}https://github.com/new${NC}"
    echo -e "  2. Nom du repo : ${GREEN}${REPO_NAME}${NC}"
    echo -e "  3. Description : ${GREEN}${DESCRIPTION}${NC}"
    echo -e "  4. Visibilité : ${GREEN}Public${NC}"
    echo -e "  5. ⚠ Ne coche PAS 'Add README' ni '.gitignore' ni 'License'"
    echo -e "  6. Clique ${GREEN}Create repository${NC}"
    echo ""
    echo -e "  Puis copie l'URL et exécute :"
    echo ""
    echo -e "  ${CYAN}git remote add origin https://github.com/TON_USERNAME/${REPO_NAME}.git${NC}"
    echo -e "  ${CYAN}git branch -M main${NC}"
    echo -e "  ${CYAN}git push -u origin main${NC}"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
fi

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     ✅ Setup terminé !                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
