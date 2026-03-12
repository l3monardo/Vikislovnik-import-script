#!/usr/bin/env node
/**
 * Node.js bridge for @interslavic/utils
 *
 * Reads JSON array of words from stdin, generates declension/conjugation
 * tables using the library, and outputs JSON results to stdout.
 *
 * Usage:
 *   echo '[{"isv":"dom","addition":"","pos":"m.","type":""}]' | node generate_tables.js
 */

const {
    declensionNoun,
    declensionAdjective,
    declensionNumeral,
    declensionPronoun,
    conjugationVerb,
    parsePos,
    transliterate,
} = require('@interslavic/utils');

function deepTransliterate(data) {
    if (typeof data === 'string') {
        return transliterate(data, 'isv-Latn');
    }
    if (Array.isArray(data)) {
        return data.map(deepTransliterate);
    }
    if (data !== null && typeof data === 'object') {
        const result = {};
        for (const [key, value] of Object.entries(data)) {
            result[key] = deepTransliterate(value);
        }
        return result;
    }
    return data;
}

function processWord(entry) {
    const { isv, addition, pos, type } = entry;
    // Strip etymological letters for processing and returning to Python
    const cleanIsv = isv ? transliterate(isv, 'isv-Latn') : '';
    const result = { word: cleanIsv, rawWord: isv, tableType: null, data: null };

    if (!pos || !isv) return result;

    let parsed;
    try {
        parsed = parsePos(pos);
    } catch (e) {
        return result;
    }

    if (!parsed) return result;

    const add = addition || '';

    switch (parsed.name) {
        case 'noun': {
            result.tableType = 'declension_noun';
            const gender = parsed.masculine ? 'masculine'
                : parsed.feminine ? 'feminine'
                    : parsed.neuter ? 'neuter'
                        : 'masculine';
            try {
                result.data = deepTransliterate(declensionNoun(
                    cleanIsv,
                    add,
                    gender,
                    parsed.animate || false,
                    parsed.plural || false,
                    parsed.singular || false,
                    parsed.indeclinable || false,
                ));
            } catch (e) {
                result.data = null;
            }
            break;
        }

        case 'adjective': {
            result.tableType = 'declension_adj';
            try {
                result.data = deepTransliterate(declensionAdjective(cleanIsv, add, pos));
            } catch (e) {
                result.data = null;
            }
            break;
        }

        case 'numeral': {
            result.tableType = 'declension_numeral';
            // Determine numeral type from parsed data
            let numeralType = 'cardinal';
            if (parsed.ordinal) numeralType = 'ordinal';
            else if (parsed.collective) numeralType = 'collective';
            else if (parsed.fractional) numeralType = 'fractional';
            else if (parsed.differential) numeralType = 'differential';
            else if (parsed.multiplicative) numeralType = 'multiplicative';
            else if (parsed.substantivized) numeralType = 'substantivized';
            // Use type column if available
            if (type) numeralType = type;
            try {
                result.data = deepTransliterate(declensionNumeral(cleanIsv, numeralType));
            } catch (e) {
                result.data = null;
            }
            break;
        }

        case 'pronoun': {
            result.tableType = 'declension_pronoun';
            // Determine pronoun type from parsed data
            let pronounType = 'personal';
            if (parsed.demonstrative) pronounType = 'demonstrative';
            else if (parsed.indefinite) pronounType = 'indefinite';
            else if (parsed.interrogative) pronounType = 'interrogative';
            else if (parsed.possessive) pronounType = 'possessive';
            else if (parsed.reflexive) pronounType = 'reflexive';
            else if (parsed.relative) pronounType = 'relative';
            else if (parsed.reciprocal) pronounType = 'reciprocal';
            // Use type column if available
            if (type) pronounType = type;
            try {
                result.data = deepTransliterate(declensionPronoun(cleanIsv, pronounType));
            } catch (e) {
                result.data = null;
            }
            break;
        }

        case 'verb': {
            result.tableType = 'conjugation';
            try {
                result.data = deepTransliterate(conjugationVerb(cleanIsv, add, pos));
            } catch (e) {
                result.data = null;
            }
            break;
        }

        default:
            // conjunction, preposition, interjection, prefix — no table
            break;
    }

    return result;
}

// Read all stdin
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
    try {
        const words = JSON.parse(input);
        const results = words.map(processWord);
        process.stdout.write(JSON.stringify(results));
    } catch (e) {
        process.stderr.write(`Error: ${e.message}\n`);
        process.exit(1);
    }
});
