#!/usr/bin/python3
# -*- coding: utf-8 -*-

import re
import nltk
import sys
import getopt
import pickle
import math
import heapq
import functools
from collections import Counter
from index import Posting, PostingList, Field
from encode import check_and_decode
from nltk.corpus import wordnet
from nltk.corpus import stopwords

# Initialise Global variables

D = {} # to store all (term to posting file cursor value) mappings
POSTINGS_FILE_POINTER = None # reference for postings file
DOC_LENGTHS = None # to store all document lengths
ALL_DOC_IDS = None # to store all doc_ids
AND_KEYWORD = "AND"
EMPHASIS_ON_ORIG = 1.0 # initial query
EMPHASIS_ON_RELDOC = 0.75 # relevant marked documents
EMPHASIS_ORIG_MULTIPLIER_POSTPROCESSING = 1.1

def comparator(tup1, tup2):
    """
    Sorts the 2 tuples by score first, then doc_id in ascending order
    """
    if tup1[0] > tup2[0]:
        return 1
    elif tup2[0] > tup1[0]:
        return -1
    else:
        return tup2[1] - tup1[1]

# Parsing
def filter_punctuations(s):
    """
    Replaces certain punctuations from Strings with space, to be removed later on
    Takes in String s and returns the processed version of it
    """
    punctuations = ''''''

    space = '''''' # please check if this will interfere with phrasal or not
    remove = ''''''

    filtered_term = ""
    for character in s:
        if character in remove:
            s = s.replace(character,"") # eg Arnold's Fried Chicken -> Arnolds Fried Chicken (more relevant) VS Arnold s Fried Chicken
        elif character in space:
            filtered_term += " "
        else:
            filtered_term += character
    return filtered_term

def process(arr):
    """
    Filters out some punctuations and then case-folds to lowercase
    Takes in a String array and returns the result array
    """
    return [filter_punctuations(term) for term in arr]

def stem_word(term):
    stemmer = nltk.stem.porter.PorterStemmer()
    return stemmer.stem(term)

def stem_query(arr):
    """
    Takes in a case-folded array of terms previously processed for punctuations, tokenises it, and returns a list of stemmed terms
    """
    return [stem_word(term) for term in arr]

# Ranking

def boost_score_based_on_field(field, score):
    # TODO: Decide on an appropriate boost value
    court_boost = 4
    title_boost = 10
    if field == Field.TITLE:
        return score * title_boost
    elif field == Field.COURT:
        return score * court_boost
    else:
        # no boost to score
        return score
    return score

def cosine_score(tokens_arr, relevant_docids):
    """
    Takes in an array of terms, and returns a list of the top scoring documents based on cosine similarity scores with respect to the query terms
    """

    # We first obtain query vector value for specific term
    # Then, we perform Rocchio Algorithm to finalise the query vector based on relevance assessments
    # Rocchio Algorithm:
    # 1. Take in original query vector value for this term
    # 2. Take in all relevant_docids vector values for this term, accumulate them and then average/normalise them
    # 3. Use this as the new query vector's value for this term
    # Once done, we calculate each term's score contribution to every one of its documents' overall score as with the standard VSM
    # This will reflect the effects of the Rocchio Algorithm

    # We normalise at the end to optimise speed.

    # Step 1: Preparation
    scores = {} # to store all cosine similarity scores for each query term
    term_frequencies = Counter(tokens_arr) # the query's count vector for every of its terms, to obtain data for pointwise multiplication
    # Set of all relevant documents' top K (already processed) terms, and query's processed terms
    # Reminder: They are ALREADY PROCESSED aka filtered for punctuations, casefolded to lowercase, stemmed
    union_of_relevant_doc_top_terms = []
    for impt in relevant_docids:
        ls = ALL_DOC_IDS[impt]
        for t in ls:
            union_of_relevant_doc_top_terms.append(t)
    processed_terms = [stem_word(w.strip().lower()) for w in tokens_arr]
    for t in processed_terms:
        union_of_relevant_doc_top_terms.append(t)
    union_of_relevant_doc_top_terms = set(union_of_relevant_doc_top_terms) # all unique now, all are processed

    # Step 2: Obtain PostingList of interest
    is_entirely_phrasal = True # if False, should perform Rocchio for Query Refinement
    for term in tokens_arr:
        # Obtain the first vector, representing all the document(s) containing the term
        # We will calculate its weight later
        # Document IDs are nicely reflected in the term's PostingList
        # Only documents with Postings of this term will have non-zero score contributions
        posting_list = None
        query_type = "YET DECIDED" # for the current query term
        if " " in term:
            # The only time we have a space in a term is when the term is one in a phrasal query
            # Otherwise it is not a phrasal query (a phrase must have >1 word!)
            query_type = "PHRASAL"
            posting_list_object = perform_phrase_query(term)
            if posting_list_object is not None:
                posting_list = posting_list_object.postings
        else:
            # Otherwise, this term is under freetext search, and we can optimise using Rocchio Algorithm
            query_type = "FREETEXT"
            posting_list = find_term(term)
            is_entirely_phrasal = False # should perform Rocchio
        if posting_list is None:
            # Invalid query term
            continue

        # Step 3: Obtain the query vector's (possibly refined) value for pointwise multiplication
        query_term_weight = get_query_weight(posting_list.unique_docids, term_frequencies[term]) # before/without Rocchio
        # Query Refinement: Rocchio Algorithm (Part 1: common terms with query)
        # Want to use given relevant documents to get entry of the term in the refined query vector
        if (query_type == "FREETEXT"):
            # We are doing query refinement for this current term; no need to do again later: remove it first!
            # current term is not processed -> Need to process first to compare
            processed_term = stem_word(term.strip().lower())
            union_of_relevant_doc_top_terms.remove(processed_term)
            # calculate the centroid value for tf for calculating refined query value for this term
            # Note: documents can have a 0 contribution for particular terms if they don't contain them
            accumulated_value = 0
            for doc_id in relevant_docids:
                # divide by doc_lengths for effective normalisation to consider distribution of the current term within the document
                accumulated_value += find_term_specific_weight_for_specified_id(doc_id, posting_list)/DOC_LENGTHS[doc_id]
            relevant_centroid_value = accumulated_value/len(relevant_docids)
            # Use the relevant 'centroid' to calculate refined query entry
            if (relevant_centroid_value > 0):
                # most of the time, it should arrive at this branch
                query_term_weight = (EMPHASIS_ON_ORIG * query_term_weight) + (EMPHASIS_ON_RELDOC * relevant_centroid_value)
                # Otherwise, we don't change query_term_weight as it is better off without, or error in Rocchio Algo value

        # Step 4: Perform scoring by pointwise multiplication for the 2 vectors
        # Accumulate all score contribution from the current term before normalisation for lnc.ltc scheme
        # Boost accordingly to fields/zones
        for posting in posting_list.postings:
            doc_term_weight = 1 + math.log(len(posting.positions), 10) # guaranteed no error in lnc calculation as tf >= 1
            if posting.doc_id not in scores:
                scores[posting.doc_id] = (boost_score_based_on_field(posting.field, doc_term_weight) * query_term_weight)
            else:
                scores[posting.doc_id] += (boost_score_based_on_field(posting.field, doc_term_weight) * query_term_weight)

    # Step 5 (Optional): Rocchio Part 2 (if needed; for unadjusted terms in top_K)
    # We begin with an initial query vector value of 0. And then we add the averaged 'centroid' value
    # which derived from the documents marked as relevant by the lawyers
    # Note these terms are already processed; we need to use find_already_processed_term(term) function

    if (is_entirely_phrasal == False):
        # for terms that have not been covered, but need to be considered by Rocchio
        while (len(union_of_relevant_doc_top_terms) > 0):

            # keep finding terms to do scoring until empty; removal by .pop()
            next_term = union_of_relevant_doc_top_terms.pop()

            # Find posting list for the term
            posting_list = find_already_processed_term(next_term)
            if posting_list is None:
                continue

            # Calculate refined query value for multiplication
            final_query_value = 0 # Initialised at 0 since the tf measure gives 0 in the ltc scheme
            for doc_id in relevant_docids:
                # this is entirely made from contributions of the relevant documents
                final_query_value += find_term_specific_weight_for_specified_id(doc_id, posting_list)/DOC_LENGTHS[doc_id]
            final_query_value = (EMPHASIS_ON_RELDOC) * final_query_value/len(relevant_docids)

            for posting in posting_list.postings:
                # Obtain weight of term for each document and perform calculation
                doc_term_weight = 1 + math.log(len(posting.positions), 10) # guaranteed no error in log calculation as tf >= 1
                if posting.doc_id not in scores:
                    scores[posting.doc_id] = (boost_score_based_on_field(posting.field, doc_term_weight) * final_query_value)
                else:
                    scores[posting.doc_id] += (boost_score_based_on_field(posting.field, doc_term_weight) * final_query_value)


    # Step 6: Perform normalisation to consider the length of the document vector
    # We save on dividing by the query vector length which is constant and does not affect score comparison
    # At this point, all scoring is done, except normalising
    doc_ids_in_tokens_arr = find_by_document_id(tokens_arr)
    results = []
    for doc_id, total_weight in scores.items():
        ranking_score = total_weight/DOC_LENGTHS[doc_id]
        # Now we check if any of the query terms matches
        # TODO: Improve the doc_id criterion below
        # Since the user searches for terms which he/she tends to want, we place higher emphasis on these
        if doc_id in doc_ids_in_tokens_arr:
            ranking_score *= EMPHASIS_ORIG_MULTIPLIER_POSTPROCESSING
        results.append((ranking_score, doc_id))

    # Step 7: Sort the results in descending order of score
    results.sort(key=functools.cmp_to_key(comparator))
    return results

def find_term_specific_weight_for_specified_id(doc_id, posting_list):
    """
    Returns the accumulated weight (regardless of field type) for the stated document of doc_id seen in posting_list (which is a PostingList for a given dictionary term)
    Score is returned in ltc scheme, following that for query
    """

    result = 0 # remains 0 if the doc_id marked relevant does not contain the term that the PostingList represents for
    tf = 0

    # scan through posting_list and accumulate to get the specified document's total tf regardless of field type
    for posting in posting_list.postings:
        if (posting.doc_id == doc_id):
            # number of positions in positional index is the number of occurrences of this term in that field
            tf += len(posting.positions)

    # if the specified document does contain the term, return ltc weight (following query), otherwise return 0
    if (tf > 0):
        df = posting_list.unique_docids
        N = len(ALL_DOC_IDS)
        result = (1 + math.log(tf, 10)) * math.log(N/df, 10)

    return result

def get_query_weight(df, tf):
    """
    Calculates the tf-idf weight for a term in the query vector
    Takes in document frequency df, term frequency tf, and returns the resulting tf-idf weight
    We treat the query as a document itself, having its own term count vector
    We use ltc in the calculation for queries, as opposed to lnc for documents
    This requires document frequency df, term frequency tf, total number of documents N
    """
    N = len(ALL_DOC_IDS)
    # df, tf and N are all guranteed to be at least 1, so no error is thrown here
    return (1 + math.log(tf, 10)) * math.log(N/df, 10)

def find_term(term):
    """
    Takes in a term, then finds and returns the list representation of the PostingList of the given term
    or an empty list if no such term exists in index
    """
    # NOTE: LOWERCASING IS ONLY DONE HERE.
    term = term.strip().lower()
    term = stem_word(term)
    if term not in D:
        return None
    POSTINGS_FILE_POINTER.seek(D[term])
    return pickle.load(POSTINGS_FILE_POINTER)

def find_already_processed_term(term):
    if term not in D:
        return None
    POSTINGS_FILE_POINTER.seek(D[term])
    return pickle.load(POSTINGS_FILE_POINTER)

def find_by_document_id(terms):
    """
    Checks if any of the query terms are document ids, if so return the document id
    To be used after the normal boolean/free text parsing
    """
    document_ids = []
    for term in terms:
        if all(map(str.isdigit, term)):
            if int(term) in ALL_DOC_IDS:
                document_ids.append(int(term))
    return document_ids

# Takes in a phrasal query in the form of an array of terms and returns the doc ids which have the phrase
# Note: Only use this for boolean retrieval, not free text mode
def perform_phrase_query(phrase_query):
    # Defensive programming, if phrase is empty, return false
    if not phrase_query:
        return False
    phrases = phrase_query.split(" ")
    phrase_posting_list = find_term(phrases[0])
    if phrase_posting_list == None:
        return None

    for term in phrases[1:]:
        current_term_postings = find_term(term)
        if current_term_postings == None:
            return None
        # Order of arguments matter
        phrase_posting_list = merge_posting_lists(phrase_posting_list, current_term_postings, True)

    return phrase_posting_list

# Returns merged positions for phrasal query
# positions2 comes from the following term and positions1 from
# the preceeding term
def merge_positions(positions1, positions2, doc_id):
    merged_positions = []
    L1 = len(positions1)
    L2 = len(positions2)
    index1, index2 = 0, 0
    offset1, offset2 = 0, 0
    # This is for our gap encoding
    last_position_of_merged_list = 0
    # Do this because we have byte encoding
    calculate_actual_pos_from_offset = lambda curr_value, offset: curr_value + offset
    while index1 < L1 and index2 < L2:
        proper_position2 = calculate_actual_pos_from_offset(positions2[index2], offset2)
        if calculate_actual_pos_from_offset(positions1[index1], offset1) + 1 == proper_position2:
            # Only merge the position of index2 because
            # We only need the position of the preceeding term
            # Need to do some math now because of our gap encoding, sadly
            position_to_append = proper_position2 - last_position_of_merged_list
            last_position_of_merged_list = proper_position2
            merged_positions.append(position_to_append)

            # Update the offsets of the original two positing lists
            offset1 += positions1[index1]
            offset2 += positions2[index2]
            index1 += 1
            index2 += 1
        elif calculate_actual_pos_from_offset(positions1[index1], offset1) + 1 > proper_position2:
            offset2 += positions2[index2]
            index2 += 1
        else:
            offset1 += positions1[index1]
            index1 += 1
    return merged_positions

# Performs merging of two posting lists
# Note: Should perform merge positions is only used for phrasal queries
# Term frequency does not matter for normal boolean queries
def merge_posting_lists(list1, list2, should_perform_merge_positions = False):
    """
    Merges list1 and list2 for the AND boolean operator
    """
    merged_list = PostingList()
    L1 = len(list1.postings)
    L2 = len(list2.postings)
    curr1, curr2 = 0, 0

    while curr1 < L1 and curr2 < L2:
        posting1 = list1.postings[curr1]
        posting2 = list2.postings[curr2]
        # If both postings have the same doc id, add it to the merged list.
        if posting1.doc_id == posting2.doc_id:
            # Order of fields is title -> court-> content
            # Now we have to merge by the postings of the different fields
            # Case 1: Both doc_id and field are the same
            if posting1.field == posting2.field:
                if should_perform_merge_positions:
                    merged_positions = merge_positions(check_and_decode(posting1.positions), check_and_decode(posting2.positions), posting1.doc_id)
                    # Only add the doc_id if the positions are not empty
                    if len(merged_positions) > 0:
                        merged_list.insert_without_encoding(posting1.doc_id, posting1.field, merged_positions)
                else:
                    merged_list.insert_posting(posting1)
                curr1 += 1
                curr2 += 1
            # Case 2: posting1's field smaller than posting2's field
            elif posting1.field < posting2.field:
                # TODO: To prove but I think this hunch is correct
                # There should not be a case where posting2 has the same field but has merged it in previously.
                # This insert should never be a duplicate
                merged_list.insert_posting(posting1)
                curr1 += 1
            # Case 3: Converse of case 2
            else:
                merged_list.insert_posting(posting2)
                curr2 += 1
        else:
            # Else if there is a opportunity to jump and the jump is less than the doc_id of the other list
            # then jump, which increments the index by the square root of the length of the list
            # if posting1.pointer != None and posting1.pointer.doc_id < posting2.doc_id:
                # curr1 = posting1.pointer.index
            # elif posting2.pointer != None and posting2.pointer.doc_id < posting1.doc_id:
                # curr2 = posting2.pointer.index
            # # If we cannot jump, then we are left with the only option of incrementing the indexes one by one
            # else:
            if posting1.doc_id < posting2.doc_id:
                curr1 += 1
            else:
                curr2 += 1
    return merged_list

def parse_query(query, relevant_docids):
    terms_array, is_boolean_query = split_query(query)
    if is_boolean_query:
        return parse_boolean_query(terms_array, relevant_docids)
    else:
        return parse_free_text_query(terms_array, relevant_docids)

def get_ranking_for_boolean_query(posting_list, relevant_docids):
    """
    The scoring for boolean queries is going to follow CSS Specificity style
    Title matches will be worth 20, court 10 and content 1 (numbers to be confirmed)
    The overall relevance of the documents would be the sum of all these scores
    Example: If the resultant posting list has two postings for doc_id xxx, with fields COURT and CONTENT
    Then the resultant score is 11
    """
    relevant_score = 100000
    title_score = 20
    court_score = 10
    content_score = 1

    def get_boolean_query_scores(field):
        if field == Field.TITLE:
            return title_score
        elif field == Field.COURT:
            return court_score
        else:
            return content_score

    scores = {}
    for posting in posting_list.postings:
        score = get_boolean_query_scores(posting.field)
        if posting.doc_id not in scores:
            scores[posting.doc_id] = score
        else:
            scores[posting.doc_id] += score

    # Add to the ones judged relevant by humans
    for relevant_docid in relevant_docids:
        if relevant_docid in scores:
            scores[relevant_docid] = relevant_score
        else:
            scores[relevant_docid] += relevant_score

    # Now we do the sorting
    sorted_results = sorted([(score, doc_id) for doc_id, score in scores.items()], key=functools.cmp_to_key(comparator))

    return sorted_results

def parse_boolean_query(terms, relevant_docids):
    """
    Takes in the array of terms from the query
    Returns the posting list of all the phrase
    """
    # First filter out all the AND keywords from the term array
    filtered_terms = [term for term in terms if term != AND_KEYWORD]

    filtered_terms = process(filtered_terms)
    # Get the posting list of the first word
    first_term = filtered_terms[0]
    res_posting_list = None
    if " " in first_term:
        res_posting_list = perform_phrase_query(first_term)
    else:
        res_posting_list = find_term(first_term)

    if res_posting_list is None:
        return []

    # Do merging for the posting lists of the rest of the terms
    for term in filtered_terms[1:]:
        term_posting_list = None
        if " " in term:
            term_posting_list = perform_phrase_query(term)
        else:
            term_posting_list = find_term(term)

        if term_posting_list is None:
            return []
        res_posting_list = merge_posting_lists(res_posting_list, term_posting_list)

    return get_ranking_for_boolean_query(res_posting_list, relevant_docids)

def parse_free_text_query(terms, relevant_docids):
    # TODO: See below (delete once done)
    #Expected to add query expansion, after process(query) is done
    #query = query_expansion(process(query))
    terms = process(terms)
    res = cosine_score(terms, relevant_docids)
    return res

def split_query(query):
    """
    split_query extracts out the terms into phrases and terms
    Assumes that the query is well formed.
    """
    start_index = 0
    is_in_phrase = False
    is_boolean_query = False
    current_index = 0
    terms = []

    while current_index < len(query):
        current_char = query[current_index]
        if current_char == "\"":
            # This is the start or end of a phrasal query term
            # Note that this phrasal query is treated like a free-text query, but on a fixed term
            # We will differentiate them later on
            if is_in_phrase:
                is_in_phrase = False
                terms.append(query[start_index:current_index]) # entire phrase as a term
                start_index = current_index + 1 # +1 to ignore the space after this
            else:
                start_index = current_index + 1
                is_in_phrase = True
        elif current_char == " ":
            # this is the end of a non-phrasal query term, can append directly
            if not is_in_phrase:
                terms.append(query[start_index:current_index])
                if (query[start_index:current_index] == AND_KEYWORD):
                    is_boolean_query = True
                start_index = current_index + 1
        current_index += 1

    # Add in the last term if it exists
    if start_index < current_index:
        terms.append(query[start_index:current_index])

    # Weed out empty strings
    return [term for term in terms if term], is_boolean_query

def query_expansion(query):
    #Split the query into words
    #Remove stop words
    #Find the synonyms of each word and append them to a set, since some of the synonyms might be repetitive
    #Add the set of synonyms to list of extended query words
    #Convert the extended query list to extende query string
    #Return the string

    query_words = query.split()
    stop_words = set(stopwords.words('english'))
    query_words = [word for word in query_words if not word in stop_words]
    expanded_query = []
    for word in query_words:
        expanded_query.append(word)
        syn_set = set()
        for s in wordnet.synsets(word):
            for l in s.lemmas():
                syn_set.add(l.name())
        expanded_query.extend(syn_set)

    new_query = ' '.join([str(word).lower() for word in expanded_query])

    return new_query
# Below are the code provided in the original Homework search.py file, with edits to run_search to use our implementation

def usage():
    print("usage: " + sys.argv[0] + " -d dictionary-file -p postings-file -q file-of-queries -o output-file-of-results")

def run_search(dict_file, postings_file, queries_file, results_file):
    """
    Perform query searches from queries file using the given dictionary file and postings file, writing results to results file
    """
    global D
    global POSTINGS_FILE_POINTER
    global DOC_LENGTHS
    global ALL_DOC_IDS

    # 1. Reading data from files into memory: File Pointer Mappings, Document Lengths, Document IDs
    dict_file_fd = open(dict_file, "rb")
    D = pickle.load(dict_file_fd) # dictionary with term:file cursor value entries
    DOC_LENGTHS = pickle.load(dict_file_fd) # dictionary with doc_id:length entries
    ALL_DOC_IDS = pickle.load(dict_file_fd) # data for optimisation, e.g. Rocchio Algo
    POSTINGS_FILE_POINTER = open(postings_file, "rb")
    # PostingLists for each term are accessed separately using file cursor values given in D
    # because they are significantly large and unsuitable for all of them to be used in-memory

    # 2. Process Queries
    with open(queries_file, "r") as q_file:
        with open(results_file, "w") as r_file:
            # TODO: Wrap these clause with Try catch block
            lines = [line.rstrip("\n") for line in q_file.readlines()]
            query = lines[0]
            relevant_docids = [int(doc_id) for doc_id in lines[1:]]
            res = []
            res = parse_query(query, relevant_docids)
            r_file.write(" ".join([str(r[1]) for r in res]) + "\n")

    # 4. Cleaning up: close files
    dict_file_fd.close()
    POSTINGS_FILE_POINTER.close()

dictionary_file = postings_file = file_of_queries = output_file_of_results = None

try:
    opts, args = getopt.getopt(sys.argv[1:], 'd:p:q:o:')
except getopt.GetoptError:
    usage()
    sys.exit(2)

for o, a in opts:
    if o == '-d':
        dictionary_file  = a
    elif o == '-p':
        postings_file = a
    elif o == '-q':
        file_of_queries = a
    elif o == '-o':
        file_of_output = a
    else:
        assert False, "unhandled option"

if dictionary_file == None or postings_file == None or file_of_queries == None or file_of_output == None :
    usage()
    sys.exit(2)

run_search(dictionary_file, postings_file, file_of_queries, file_of_output)
