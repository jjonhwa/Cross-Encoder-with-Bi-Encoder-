import torch
import pandas as pd

from tqdm import tqdm
from datasets import load_from_disk
from transformers import AutoTokenizer

from utils import Passage_Embedding
from encoder import BertEncoder_For_BiEncoder, RoBertaEncoder_For_CrossEncoder


class Retrieve_By_BiEncoder:
    def __init__(self, p_encoder, q_encoder, wiki_path, tokenizer):
        self.p_encoder = p_encoder
        self.q_enocder = q_encoder
        self.wiki_path = wiki_path
        self.tokenizer = tokenizer

        self.corpus = Passage_Embedding(wiki_path, p_encoder).corpus
        # Acquiring passage embedding can take a while (based on about 10-20 minutes, 60000 corpus),
        #  and if you want to shorten the time, it is recommended to save passage embedding as a bin file and bring it in and use it.
        self.p_embs = Passage_Embedding.get_passage_embedding(tokenizer)

    def get_relavant_doc(self, queries, k=1):
        with torch.no_grad():
            self.q_encoder.eval()
            q_seqs_val = self.tokenizer(
                queries, padding="max_length", truncation=True, return_tensors="pt"
            )

            if torch.cuda.is_available():
                q_seqs_val = q_seqs_val.to("cuda")
            q_emb = self.q_encoder(**q_seqs_val).to("cpu")

        dot_prod_scores = torch.mm(q_emb, self.p_embs.T)
        sort_result = torch.sort(dot_prod_scores, dim=1, descending=True)

        scores, ranks = sort_result[0], sort_result[1]

        result_scores = []
        result_indices = []
        for i in range(len(ranks)):
            result_scores.append(scores[i].tolist()[:k])
            result_indices.append(ranks[i].tolist()[:k])

        return result_scores, result_indices


def get_retrieval_acc(dataset, corpus, doc_indices):
    """
    The k passages that have passed through the Retrieval are made into DataFrame,
    and the retrival accuracy is calculated using them.

    This code is implemented according to the data used during this process,
    and will be modified and proceeded according to the data.
    """
    total = []
    for idx, example in enumerate(dataset):
        tmp = {
            # Query와 해당 id를 반환합니다.
            "question": example["question"],
            "id": example["id"],
            # Retrieve한 Passage의 id, context를 반환합니다.
            "context_id": doc_indices[idx],
            "context": " ".join(  # 기존에는 ' '.join()
                [corpus[pid] for pid in doc_indices[idx]]
            ),
        }
        if "context" in example.keys() and "answers" in example.keys():
            # validation 데이터를 사용하면 ground_truth context와 answer도 반환합니다.
            tmp["original_context"] = example["context"]
            tmp["answers"] = example["answers"]
        total.append(tmp)

    dataframe = pd.DataFrame(total)

    correct_length = []
    for i in range(len(dataframe)):
        if dataframe["original_context"][i] in dataframe["context"][i]:
            correct_length.append(i)

    return len(correct_length) / len(dataset)


def rerank(queries, c_encoder, doc_indices):
    """
    Passage returned from the bi-encoder is re-ranked using the cross encoder.

    Args:
        queries: Questions in validation or test data.
        c_encoder: Trained cross encoder
        doc_indices: Index number in corpus of Top-k passages retrieved from bi-encoder

    Return:
        rsult_indices -> List : index number in corpus of passesages re-ranked from crossencoder
    """
    with torch.no_grad():
        c_encoder.eval()

        result_scores = []
        result_indices = []
        for i in tqdm(range(len(queries))):
            question = queries[i]
            question_score = []

            for indice in tqdm(doc_indices[i]):
                passage = corpus[indice]
                tokenized_examples = tokenizer(
                    question,
                    passage,
                    truncation="only_second",
                    max_length=512,
                    stride=128,
                    return_overflowing_tokens=True,
                    return_offsets_mapping=True,
                    # return_token_type_ids=False,  # roberta모델을 사용할 경우 False, bert를 사용할 경우 True로 표기해야합니다.
                    padding="max_length",
                    return_tensors="pt",
                )
                score = 0
                for i in range(len(tokenized_examples["input_ids"])):
                    input_ids = torch.tensor(
                        tokenized_examples["input_ids"][i].unsqueeze(dim=0)
                    )
                    attention_mask = torch.tensor(
                        tokenized_examples["attention_mask"][i].unsqueeze(dim=0)
                    )
                    token_type_ids = torch.tensor(
                        tokenized_examples["token_type_ids"][i].unsqueeze(dim=0)
                    )

                    if torch.cuda.is_avaliable():
                        input_ids = input_ids.to("cuda")
                        attention_mask = attention_mask.to("cuda")
                        token_type_ids = token_type_ids.to("cuda")

                    c_input = {
                        "input_ids": input_ids,
                        "attention_mask": attention_mask,
                        "token_type_ids": token_type_ids,
                    }

                    tmp_score = c_encoder(**c_input)
                    if torch.cuda.is_available():
                        tmp_score = tmp_score.to("cpu")
                    score += tmp_score

                score = score / len(tokenized_examples["input_ids"])
                question_score.append(score)

            sort_result = torch.sort(torch.tensor(question_score), descending=True)
            scores, index_list = sort_result[0], sort_result[1]

            result_scores.append(scores.tolist())
            result_indices.append(index_list.tolist())

    return result_scores, result_indices


if __name__ == "__main__":

    # q_encoder & p_encoder are called only when BertEncoder is defined.
    BertEncoder = BertEncoder_For_BiEncoder
    p_encoder = torch.load("/save_directory/p_encoder.pt")
    q_encoder = torch.load("/save_directory/q_encoder.pt")
    wiki_path = "/save_directory/wiki_data.json"

    model_checkpoint = "klue/bert-base"
    tokenizer = AutoTokenizer.from_pretrained(model_checkpoint)

    # Bi-Encoder Retrieval (First Scoring)
    biencoder_retrieval = Retrieve_By_BiEncoder(
        p_encoder, q_encoder, wiki_path, tokenizer
    )  # get corpus & p_embs

    corpus = biencoder_retrieval.corpus
    dataset = load_from_disk("../data/train_dataset")
    queries = dataset["validation"][
        "question"
    ]  # dataset has valid/train data and We will calculate the score for the validation set.

    doc_scores, doc_indices = biencoder_retrieval.get_relavant_doc(queries, k=500)
    # k usually utilizes 20, 50, and 100, and since this code will re-rank it with a cross encoder,
    # 500 was given to obtain the highest retrival acc.
    # (It may be larger than 500, but it consumes considerable resources when passing through the cross encoder.)

    biencoder_retrieval_acc = get_retrieval_acc(
        dataset["validataion"], corpus, doc_indices
    )
    print("BiEncoder Retrieval Accuracy:", biencoder_retrieval_acc)

    # Cross-Encoder Retrieval (Re-Ranking)
    # c_encoder is called only when RoBertaEncoder is defined.
    # (In this process, RobertaEncoder is defined because c_encoder using Roberta is called. If a c_encoder using abert is called, then a BertEncoder is defined.)
    RoBertaEncoder = RoBertaEncoder_For_CrossEncoder
    c_encoder = torch.load("/save_directory/c_encoder.pt")
    result_scores, result_indices = rerank(queries, c_encoder, doc_indices)

    # get final Top-k Passages: Here, I just get 50 passage
    final_indices = []
    for i in range(len(doc_indices)):
        t_list = [doc_indices[i][result_indices[i][k]] for k in range(50)]
        final_indices.append(t_list)

    crossencoder_retrieval_acc = get_retrieval_acc(
        dataset["validation"], corpus, final_indices
    )
    print("CrossEncoder Retrieval Accuracy:", crossencoder_retrieval_acc)
